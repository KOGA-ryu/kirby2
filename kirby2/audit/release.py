"""Executable audits and immutable-evidence validators for Work Order 40.

The release audit layer exercises public production contracts.  It does not contain
another release runtime, build implementation, or qualification workload.  WO40-F
through WO40-I are evidence-only gates: before their exact committed evidence exists
they report ``NOT_EXERCISED`` and never attempt the one-time work themselves.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import io
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from typing import Iterable, Mapping

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes


WO40A_AUDIT_CASE_COUNT = 4
WO40B_AUDIT_CASE_COUNT = 4
WO40B1_AUDIT_CASE_COUNT = 3
WO40C_AUDIT_CASE_COUNT = 3
WO40D_AUDIT_CASE_COUNT = 4
WO40D1_AUDIT_CASE_COUNT = 2
WO40E_AUDIT_CASE_COUNT = 4
DEV0009_AUDIT_CASE_COUNT = 3
DEV0010_AUDIT_CASE_COUNT = 2
DEV0011_AUDIT_CASE_COUNT = 5
DEV0012_AUDIT_CASE_COUNT = 4
DEV0013_AUDIT_CASE_COUNT = 3
DEV0014_AUDIT_CASE_COUNT = 3
DEV0015_AUDIT_CASE_COUNT = 4

_DEV0011_PREDECESSOR_COMMIT_V1 = "da9612349db2f76863ee16fb7726c6d8f85f5329"
_DEV0011_SOURCE_MANIFEST_SHA256_V1 = (
    "f186ce81046e5c235fd80b24428e6d18dd680b33c8279e602b82fcfc524d2d98"
)
_DEV0011_PROTOCOL_SET_SHA256_V1 = (
    "94f0050a592e3279a4b38b3d2e55b0ccfdc784202e67dca13e21a91fb631f9e8"
)

RELEASE_GATE_EVIDENCE_SCHEMA_ID_V1 = "KIRBY2_RELEASE_GATE_EVIDENCE_V1"
RELEASE_CLOSEOUT_PREREQUISITE_PACKET_SCHEMA_ID_V1 = (
    "KIRBY2_RELEASE_CLOSEOUT_PREREQUISITE_PACKET_V1"
)
RELEASE_EVIDENCE_MARKER_START_V1 = (
    "<!-- KIRBY2_RELEASE_GATE_EVIDENCE_V1\n"
)
RELEASE_EVIDENCE_MARKER_END_V1 = (
    "\nKIRBY2_RELEASE_GATE_EVIDENCE_V1 -->"
)

RELEASE_FUTURE_EVIDENCE_PATHS_V1: Mapping[str, str] = {
    "WO40-F": "KIRBY2_RELEASE_BUILD_EVIDENCE.md",
    "WO40-G": "KIRBY2_RELEASE_MACOS_EVIDENCE.md",
    "WO40-H": "KIRBY2_RELEASE_LINUX_EVIDENCE.md",
    "WO40-I": "KIRBY2_RELEASE_PERFORMANCE_EVIDENCE.md",
}

RELEASE_REQUIRED_DEVIATION_GATE_IDS_V1 = tuple(
    f"DEV-{ordinal:04d}" for ordinal in range(1, 16)
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,255}\Z")


class ReleaseAuditStatus(str, Enum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"
    NOT_EXERCISED = "NOT_EXERCISED"


@dataclass(frozen=True, slots=True)
class ReleaseAuditCase:
    name: str
    detail: str
    evidence: dict[str, object]
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    exercised: bool = True
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.detail:
            raise ValueError("release audit case identity and detail are required")
        if type(self.evidence) is not dict:
            raise TypeError("release audit evidence must be an object")
        if self.failures and self.warnings:
            raise ValueError("release audit case cannot fail and warn together")
        if not self.exercised and (self.failures or self.warnings):
            raise ValueError("unexercised release audit case cannot carry results")
        if not self.exercised and not self.reason_code:
            raise ValueError("unexercised release audit case needs a reason code")

    @property
    def status(self) -> ReleaseAuditStatus:
        if not self.exercised:
            return ReleaseAuditStatus.NOT_EXERCISED
        if self.failures:
            return ReleaseAuditStatus.FAIL
        if self.warnings:
            return ReleaseAuditStatus.PASS_WITH_WARNINGS
        return ReleaseAuditStatus.PASS

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "reason_code": self.reason_code,
            "status": self.status.value,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ReleaseAuditSuite:
    gate_id: str
    cases: tuple[ReleaseAuditCase, ...]
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.gate_id or not self.cases:
            raise ValueError("release audit suite requires a gate and cases")
        names = tuple(item.name for item in self.cases)
        if len(names) != len(set(names)):
            raise ValueError("release audit case names must be unique")

    @property
    def status(self) -> ReleaseAuditStatus:
        statuses = tuple(item.status for item in self.cases)
        if ReleaseAuditStatus.FAIL in statuses:
            return ReleaseAuditStatus.FAIL
        if ReleaseAuditStatus.NOT_EXERCISED in statuses:
            return ReleaseAuditStatus.NOT_EXERCISED
        if ReleaseAuditStatus.PASS_WITH_WARNINGS in statuses:
            return ReleaseAuditStatus.PASS_WITH_WARNINGS
        return ReleaseAuditStatus.PASS

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{case.name}: {failure}"
            for case in self.cases
            for failure in case.failures
        )

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            f"{case.name}: {warning}"
            for case in self.cases
            for warning in case.warnings
        )

    @property
    def reason_code(self) -> str | None:
        reasons = tuple(
            item.reason_code
            for item in self.cases
            if item.status is ReleaseAuditStatus.NOT_EXERCISED
        )
        return reasons[0] if reasons else None


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _case(
    name: str,
    detail: str,
    evidence: dict[str, object],
    failures: Iterable[str],
) -> ReleaseAuditCase:
    return ReleaseAuditCase(
        name=name,
        detail=detail,
        evidence=evidence,
        failures=tuple(failures),
    )


def _not_exercised_case(name: str, detail: str, missing: str) -> ReleaseAuditCase:
    return ReleaseAuditCase(
        name=name,
        detail=detail,
        evidence={"missing": missing},
        exercised=False,
        reason_code="EVIDENCE_NOT_EXERCISED",
    )


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def audit_release_data_and_migrations() -> ReleaseAuditSuite:
    """Exercise the single path map, schema inventory, and migration safety."""

    from kirby2.release.migrations import (
        ReleaseMigrationPlanV1,
        ReleaseMigrationRefusalCodeV1,
        ReleaseMigrationRefused,
        ReleaseMigrationTargetV1,
        apply_release_migration,
    )
    from kirby2.release.models import (
        ReleaseSchemaInventoryV1,
        ReleaseSchemaKindV1,
        ReleaseSchemaUseV1,
        builtin_release_schema_inventory,
    )
    from kirby2.release.platform_paths import (
        ReleasePathModeV1,
        select_release_paths,
    )
    from kirby2.research.paths import DataAreaId, DataPaths

    with TemporaryDirectory(prefix="kirby2-release-audit-a-") as temporary:
        root = Path(temporary).resolve()
        selection = select_release_paths(
            ReleasePathModeV1.PORTABLE,
            explicit_root=root / "portable",
            platform_id="linux",
        )
        paths = selection.paths
        path_failures: list[str] = []
        areas = tuple(paths.area(item) for item in DataAreaId)
        if type(paths) is not DataPaths:
            path_failures.append("release selector did not return the sole DataPaths type")
        if len(tuple(DataAreaId)) != 16 or len(set(areas)) != 16:
            path_failures.append("governed release area inventory is not exactly 16 unique paths")
        if any(path.parent != paths.root for path in areas):
            path_failures.append("a governed writable area escaped the selected data root")
        if any(path.exists() for path in areas):
            path_failures.append("path selection wrote before an explicit ensure boundary")
        path_case = _case(
            "single_contained_platform_path_provider",
            "Portable selection returns the one non-writing 16-area DataPaths map.",
            {
                "area_count": len(areas),
                "mode": selection.mode.value,
                "source": selection.source,
            },
            path_failures,
        )

        inventory = builtin_release_schema_inventory(
            source_revision="release-audit-source",
            source_sha256="1" * 64,
        )
        restored = ReleaseSchemaInventoryV1.from_canonical_bytes(
            inventory.canonical_bytes()
        )
        inventory_failures: list[str] = []
        if restored != inventory or len(inventory.schemas) != 10:
            inventory_failures.append("the exact ten-schema inventory did not round-trip")
        try:
            inventory.require_supported(
                ReleaseSchemaKindV1.ENGINE,
                ReleaseSchemaUseV1.READ,
                inventory.schema(ReleaseSchemaKindV1.ENGINE).current_version + 1,
            )
        except ValueError as error:
            if "future schema" not in str(error):
                inventory_failures.append("future-schema refusal lacks recovery context")
        else:
            inventory_failures.append("an unknown future engine schema was accepted")
        inventory_case = _case(
            "closed_schema_inventory_and_future_refusal",
            "All ten compatibility kinds round-trip and future schemas fail closed.",
            {"inventory_sha256": inventory.sha256, "schema_count": len(inventory.schemas)},
            inventory_failures,
        )

        paths.ensure((DataAreaId.CONFIG,))
        source = canonical_json_bytes({"schema_version": 1, "value": "before"})
        destination = canonical_json_bytes({"schema_version": 1, "value": "after"})
        target_path = paths.config / "settings.json"
        target_path.write_bytes(source)
        schema = inventory.schema(ReleaseSchemaKindV1.ENGINE)
        target = ReleaseMigrationTargetV1(
            target_id="release-audit-settings",
            area_id=DataAreaId.CONFIG,
            relative_path="settings.json",
            schema_kind=ReleaseSchemaKindV1.ENGINE,
            source_schema_id=schema.schema_id,
            source_schema_version=schema.current_version,
            source_sha256=_sha256(source),
            destination_schema_id=schema.schema_id,
            destination_schema_version=schema.current_version,
            destination_sha256=_sha256(destination),
        )
        plan = ReleaseMigrationPlanV1(
            source_inventory=inventory,
            destination_inventory=inventory,
            targets=(target,),
        )
        receipt = apply_release_migration(
            plan,
            {target.target_id: destination},
            paths=paths,
        )
        repeated = apply_release_migration(
            plan,
            {target.target_id: destination},
            paths=paths,
        )
        backup_root = paths.backups / "migrations" / plan.migration_id
        migration_failures: list[str] = []
        if target_path.read_bytes() != destination:
            migration_failures.append("migration destination bytes differ")
        if receipt != repeated:
            migration_failures.append("completed migration was not idempotent")
        if not (backup_root / "manifest.json").is_file():
            migration_failures.append("verified pre-migration backup is absent")
        if receipt.backup_manifest_sha256 != _sha256(
            (backup_root / "manifest.json").read_bytes()
        ):
            migration_failures.append("receipt is not bound to the published backup")
        migration_case = _case(
            "backup_first_digest_bound_idempotent_migration",
            "A real mutable target is backed up, replaced, verified, and resumed exactly.",
            {
                "migration_id": plan.migration_id,
                "receipt_sha256": receipt.sha256,
                "target_sha256": _sha256(target_path.read_bytes()),
            },
            migration_failures,
        )

        refusal_failures: list[str] = []
        try:
            ReleaseMigrationTargetV1(
                target_id="release-audit-immutable",
                area_id=DataAreaId.RUNS,
                relative_path="run.json",
                schema_kind=ReleaseSchemaKindV1.ENGINE,
                source_schema_id=schema.schema_id,
                source_schema_version=schema.current_version,
                source_sha256=_sha256(source),
                destination_schema_id=schema.schema_id,
                destination_schema_version=schema.current_version,
                destination_sha256=_sha256(destination),
            )
        except ReleaseMigrationRefused as error:
            if error.code is not ReleaseMigrationRefusalCodeV1.IMMUTABLE_TARGET:
                refusal_failures.append("immutable target used the wrong refusal code")
        else:
            refusal_failures.append("immutable run evidence was accepted as a migration target")

        wrong_root = root / "wrong-source"
        wrong_paths = DataPaths(wrong_root)
        wrong_paths.ensure((DataAreaId.CONFIG,))
        wrong_target_path = wrong_paths.config / "settings.json"
        wrong_target_path.write_bytes(source)
        wrong_target = ReleaseMigrationTargetV1(
            target_id="release-audit-wrong-source",
            area_id=DataAreaId.CONFIG,
            relative_path="settings.json",
            schema_kind=ReleaseSchemaKindV1.ENGINE,
            source_schema_id=schema.schema_id,
            source_schema_version=schema.current_version,
            source_sha256="f" * 64,
            destination_schema_id=schema.schema_id,
            destination_schema_version=schema.current_version,
            destination_sha256=_sha256(destination),
        )
        wrong_plan = ReleaseMigrationPlanV1(
            source_inventory=inventory,
            destination_inventory=inventory,
            targets=(wrong_target,),
        )
        try:
            apply_release_migration(
                wrong_plan,
                {wrong_target.target_id: destination},
                paths=wrong_paths,
            )
        except ReleaseMigrationRefused as error:
            if error.code is not ReleaseMigrationRefusalCodeV1.SOURCE_DIGEST_MISMATCH:
                refusal_failures.append("source tamper used the wrong refusal code")
        else:
            refusal_failures.append("source digest mismatch was accepted")
        if wrong_target_path.read_bytes() != source:
            refusal_failures.append("failed migration changed its source target")
        refusal_case = _case(
            "immutable_and_corrupt_migration_inputs_fail_closed",
            "Immutable evidence and source-digest mismatch refuse before mutation.",
            {"immutable_area": DataAreaId.RUNS.value, "source_unchanged": True},
            refusal_failures,
        )

    cases = (path_case, inventory_case, migration_case, refusal_case)
    if len(cases) != WO40A_AUDIT_CASE_COUNT:
        raise RuntimeError("WO40-A release audit inventory changed")
    return ReleaseAuditSuite("WO40-A", cases)


def _live_recovery_fixture(root: Path):
    from kirby2.release.recovery import InteractiveRecoveryCoordinatorV1
    from kirby2.research.paths import DataPaths
    from kirby2.scenarios.market import get_scenario_definition
    from kirby2.session.bindings import BindingMap
    from kirby2.session.journal import LiveSessionSourceV1
    from kirby2.session.layouts import HotkeyLayout
    from kirby2.session.live import LiveMarketSession

    paths = DataPaths(root)
    session = LiveMarketSession(
        get_scenario_definition("balanced"), seed=42, duration_seconds=1
    )
    bindings = BindingMap.default()
    layout = HotkeyLayout.default()
    source = LiveSessionSourceV1.from_session(
        session,
        bindings,
        layout_name=layout.name,
    )
    return (
        InteractiveRecoveryCoordinatorV1(paths),
        session,
        source,
        bindings,
        layout,
    )


def audit_release_recovery() -> ReleaseAuditSuite:
    """Exercise exact continuation and fail-closed crash boundaries."""

    from kirby2.release.recovery import (
        RecoveryDispositionV1,
        RecoveryReasonCodeV1,
    )
    from kirby2.scenarios.market import get_scenario_definition
    from kirby2.session.live import LiveMarketSession

    cases: list[ReleaseAuditCase] = []
    with TemporaryDirectory(prefix="kirby2-release-audit-b-") as temporary:
        root = Path(temporary).resolve()
        coordinator, session, source, bindings, layout = _live_recovery_fixture(
            root / "exact"
        )
        initial = coordinator.inspect(source)
        journal = coordinator.start_new(
            session=session,
            source=source,
            layout=layout,
        )
        exact = coordinator.inspect(source)
        failures: list[str] = []
        if initial.disposition is not RecoveryDispositionV1.NO_RECOVERY:
            failures.append("clean data root offered a fabricated recovery")
        if exact.disposition is not RecoveryDispositionV1.EXACT_CONTINUATION:
            failures.append("complete checkpoint was not offered for exact continuation")
        cases.append(
            _case(
                "startup_offer_tracks_durable_cut",
                "A clean root offers start-new and a complete checkpoint offers exact continuation.",
                {
                    "after": exact.reason_code.value,
                    "before": initial.reason_code.value,
                    "record_count": len(journal.records),
                },
                failures,
            )
        )

        expected_state = session.state_sha256()
        restored = LiveMarketSession(
            get_scenario_definition("balanced"), seed=42, duration_seconds=1
        )
        coordinator.continue_exact(
            session=restored,
            source=source,
            bindings=bindings,
        )
        cases.append(
            _case(
                "exact_continuation_restores_complete_state",
                "Fresh-session restoration reproduces the complete durable state digest.",
                {
                    "expected_state_sha256": expected_state,
                    "restored_state_sha256": restored.state_sha256(),
                },
                (
                    ()
                    if restored.state_sha256() == expected_state
                    else ("restored session state differs from its checkpoint",)
                ),
            )
        )

        pending_coordinator, pending_session, pending_source, _, pending_layout = (
            _live_recovery_fixture(root / "pending")
        )
        pending_journal = pending_coordinator.start_new(
            session=pending_session,
            source=pending_source,
            layout=pending_layout,
        )
        pending_journal.begin_action(
            session=pending_session,
            key="d",
            command=None,
        )
        pending = pending_coordinator.inspect(pending_source)
        pending_failures: list[str] = []
        if pending.disposition is not RecoveryDispositionV1.SAFE_REPLAY_ONLY:
            pending_failures.append("pending action incorrectly allowed exact continuation")
        if pending.reason_code is not RecoveryReasonCodeV1.ACTION_ACKNOWLEDGEMENT_PENDING:
            pending_failures.append("pending action recovery reason is not explicit")
        cases.append(
            _case(
                "pending_acknowledgement_is_safe_replay_only",
                "An action at the crash boundary cannot be guessed or continued exactly.",
                {"disposition": pending.disposition.value, "reason": pending.reason_code.value},
                pending_failures,
            )
        )

        corrupt_coordinator, corrupt_session, corrupt_source, _, corrupt_layout = (
            _live_recovery_fixture(root / "corrupt")
        )
        corrupt_journal = corrupt_coordinator.start_new(
            session=corrupt_session,
            source=corrupt_source,
            layout=corrupt_layout,
        )
        journal_files = tuple(
            path
            for path in corrupt_coordinator.paths.checkpoints.rglob("*")
            if path.is_file() and path.name.endswith(".jsonl")
        )
        if not journal_files:
            raise RuntimeError("recovery audit could not locate its journal fixture")
        with journal_files[0].open("ab") as stream:
            stream.write(b"{corrupt\n")
        corrupt = corrupt_coordinator.inspect(corrupt_source)
        corrupt_failures: list[str] = []
        if corrupt.disposition is not RecoveryDispositionV1.SAFE_REPLAY_ONLY:
            corrupt_failures.append("corrupt recovery metadata did not fail closed")
        if corrupt.reason_code is not RecoveryReasonCodeV1.JOURNAL_CORRUPT:
            corrupt_failures.append("corrupt journal reason code differs")
        cases.append(
            _case(
                "corrupt_recovery_metadata_fails_closed",
                "Corrupt durable metadata offers replay or abandonment, never guessed continuation.",
                {
                    "disposition": corrupt.disposition.value,
                    "record_count": len(corrupt_journal.records),
                    "reason": corrupt.reason_code.value,
                },
                corrupt_failures,
            )
        )

    result = tuple(cases)
    if len(result) != WO40B_AUDIT_CASE_COUNT:
        raise RuntimeError("WO40-B release audit inventory changed")
    return ReleaseAuditSuite("WO40-B", result)


def audit_release_backup_restore() -> ReleaseAuditSuite:
    """Exercise portable restore plus conflict, reference, and tamper refusals."""

    from kirby2.release.backup import (
        BackupRefused,
        BackupSelectionV1,
        DatasetBackupPolicyV1,
        create_backup,
        verify_backup,
    )
    from kirby2.release.restore import RestoreRefused, restore_backup
    from kirby2.research.paths import DataAreaId, DataPaths

    with TemporaryDirectory(prefix="kirby2-release-audit-b1-") as temporary:
        root = Path(temporary).resolve()
        source_paths = DataPaths(root / "source")
        source_paths.ensure((DataAreaId.CONFIG, DataAreaId.EVIDENCE, DataAreaId.DATASETS))
        config_raw = canonical_json_bytes(
            {"profile_id": "profile-release-audit", "seed": 42}
        )
        evidence_raw = canonical_json_bytes(
            {"annotation": "portable release audit", "seed": 42}
        )
        dataset_raw = canonical_json_bytes({"dataset": "referenced", "seed": 42})
        (source_paths.config / "profiles").mkdir()
        (source_paths.config / "profiles" / "audit.json").write_bytes(config_raw)
        (source_paths.evidence / "annotations").mkdir()
        (source_paths.evidence / "annotations" / "audit.json").write_bytes(evidence_raw)
        (source_paths.datasets / "audit.json").write_bytes(dataset_raw)

        backup_root = root / "backup"
        backup = create_backup(
            paths=source_paths,
            selection=BackupSelectionV1.all_portable(
                dataset_policy=DatasetBackupPolicyV1.REFERENCE
            ),
            destination=backup_root,
        )
        verified = verify_backup(backup_root)
        destination_paths = DataPaths(root / "restored")
        restored = restore_backup(
            backup_root=backup_root,
            destination_paths=destination_paths,
            reference_paths=source_paths,
        )
        valid_failures: list[str] = []
        if verified.manifest != backup.manifest:
            valid_failures.append("published backup did not verify to its manifest")
        if (destination_paths.config / "profiles" / "audit.json").read_bytes() != config_raw:
            valid_failures.append("configuration bytes changed during restore")
        if (destination_paths.evidence / "annotations" / "audit.json").read_bytes() != evidence_raw:
            valid_failures.append("evidence bytes changed during restore")
        valid_case = _case(
            "selected_portable_state_restores_exactly",
            "A verified backup restores selected bytes into a separate clean root.",
            {
                "manifest_verified": verified.manifest == backup.manifest,
                "status": restored.receipt.status.value,
                "target_count": len(restored.receipt.targets),
            },
            valid_failures,
        )

        conflict_paths = DataPaths(root / "conflict")
        conflict_paths.ensure((DataAreaId.CONFIG,))
        (conflict_paths.config / "profiles").mkdir()
        conflict_target = conflict_paths.config / "profiles" / "audit.json"
        conflict_target.write_bytes(b"conflicting-user-bytes")
        before_conflict = conflict_target.read_bytes()
        conflict_failures: list[str] = []
        try:
            restore_backup(
                backup_root=backup_root,
                destination_paths=conflict_paths,
                reference_paths=source_paths,
            )
        except RestoreRefused:
            pass
        else:
            conflict_failures.append("conflicting destination content was overwritten")
        if conflict_target.read_bytes() != before_conflict:
            conflict_failures.append("failed restore changed the conflicting destination")

        missing_reference_paths = DataPaths(root / "missing-reference")
        try:
            restore_backup(
                backup_root=backup_root,
                destination_paths=missing_reference_paths,
            )
        except RestoreRefused:
            pass
        else:
            conflict_failures.append("digest-referenced dataset restored without its source")
        if missing_reference_paths.root.exists():
            conflict_failures.append("failed reference restore activated destination content")
        conflict_case = _case(
            "conflict_and_missing_reference_fail_non_destructively",
            "Existing bytes and absent referenced datasets refuse without partial activation.",
            {"conflict_unchanged": True, "missing_reference_root_absent": True},
            conflict_failures,
        )

        included_objects = tuple(
            path
            for path in backup_root.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        )
        if not included_objects:
            raise RuntimeError("backup audit fixture did not include an object")
        source_before = {
            path.relative_to(source_paths.root).as_posix(): _sha256(path.read_bytes())
            for path in source_paths.root.rglob("*")
            if path.is_file()
        }
        with included_objects[0].open("ab") as stream:
            stream.write(b"tamper")
        tamper_failures: list[str] = []
        try:
            verify_backup(backup_root)
        except BackupRefused:
            pass
        else:
            tamper_failures.append("tampered backup object verified")
        tamper_destination = DataPaths(root / "tamper-destination")
        try:
            restore_backup(
                backup_root=backup_root,
                destination_paths=tamper_destination,
                reference_paths=source_paths,
            )
        except RestoreRefused:
            pass
        else:
            tamper_failures.append("tampered backup restored")
        if tamper_destination.root.exists():
            tamper_failures.append("tampered restore activated destination content")
        source_after = {
            path.relative_to(source_paths.root).as_posix(): _sha256(path.read_bytes())
            for path in source_paths.root.rglob("*")
            if path.is_file()
        }
        if source_after != source_before:
            tamper_failures.append("backup tamper handling modified source data")
        tamper_case = _case(
            "tampered_backup_refuses_without_source_or_destination_mutation",
            "Object-digest tamper is detected before restore activation.",
            {"source_inventory_unchanged": source_after == source_before},
            tamper_failures,
        )

    cases = (valid_case, conflict_case, tamper_case)
    if len(cases) != WO40B1_AUDIT_CASE_COUNT:
        raise RuntimeError("WO40-B1 release audit inventory changed")
    return ReleaseAuditSuite("WO40-B1", cases)


def audit_release_first_run() -> ReleaseAuditSuite:
    """Exercise the complete offline first-run and redacted diagnostic flow."""

    from unittest.mock import patch

    from kirby2.release.commands import RELEASE_DATA_COMMAND_MODULE
    from kirby2.release.diagnostics import (
        build_release_diagnostics,
        export_release_diagnostics,
        preview_release_diagnostics,
    )
    from kirby2.release.first_run import run_first_run
    from kirby2.research.paths import DataAreaId, DataPaths

    with TemporaryDirectory(prefix="kirby2-release-audit-c-") as temporary:
        root = Path(temporary).resolve()
        paths = DataPaths(root / "data")
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = run_first_run(paths, seed=42)
        first_run_failures: list[str] = []
        if not report.complete:
            first_run_failures.append("clean offline first-run flow is incomplete")
        if len(report.writable_checks) != len(tuple(DataAreaId)):
            first_run_failures.append("first run did not check every governed path")
        if report.demonstration.status != "PASS":
            first_run_failures.append("starter place/cancel demonstration failed")
        entries = report.starter_set.get("entries")
        if type(entries) is not list or len(entries) != 2:
            first_run_failures.append("first run did not bind the two-pack starter set")
        first_run_case = _case(
            "clean_offline_first_run_is_complete",
            "Every data path, health check, starter pack, and place/cancel step completes offline.",
            {
                "demo_event_stream_sha256": report.demonstration.event_stream_sha256,
                "starter_set_sha256": report.starter_set.get("entries_sha256"),
                "writable_check_count": len(report.writable_checks),
            },
            first_run_failures,
        )

        direct_marker = b"release-audit-person@example.invalid"
        secret_marker = b"release-audit-secret-token"
        paths.identity_mappings.mkdir(exist_ok=True)
        (paths.identity_mappings / "direct.txt").write_bytes(direct_marker)
        diagnostics = build_release_diagnostics(paths)
        preview = preview_release_diagnostics(diagnostics)
        exported_path = root / "diagnostics.json"
        receipt = export_release_diagnostics(paths, exported_path)
        raw = exported_path.read_bytes()
        diagnostic_failures: list[str] = []
        if raw != diagnostics.canonical_bytes():
            diagnostic_failures.append("exported diagnostics differ from the previewed object")
        if receipt.diagnostics_sha256 != preview.diagnostics_sha256:
            diagnostic_failures.append("diagnostic receipt differs from preview identity")
        if direct_marker in raw or secret_marker in raw:
            diagnostic_failures.append("diagnostics leaked a forbidden marker")
        redactions = {
            item.field_class: item.disposition for item in diagnostics.redactions
        }
        if redactions.get("DIRECT_IDENTITY") != "EXCLUDED" or redactions.get(
            "SECRETS_AND_CREDENTIALS"
        ) != "EXCLUDED":
            diagnostic_failures.append("direct identity or secret redaction is not explicit")
        diagnostic_case = _case(
            "diagnostics_are_allowlisted_previewed_and_redacted",
            "Diagnostic bytes are explicitly previewed, new-file-only, and exclude direct identity.",
            {
                "byte_count": len(raw),
                "diagnostics_sha256": _sha256(raw),
                "redaction_count": len(diagnostics.redactions),
            },
            diagnostic_failures,
        )

        required_commands = (
            "doctor",
            "version",
            "data-paths",
            "verify-installation",
            "export-diagnostics",
        )
        command_names = tuple(item.name for item in RELEASE_DATA_COMMAND_MODULE.commands)
        command_failures: list[str] = []
        if any(command_names.count(name) != 1 for name in required_commands):
            command_failures.append("release diagnostic command registration differs")
        imported_roots: set[str] = set()
        for path in (
            _repository_root() / "kirby2/release/first_run.py",
            _repository_root() / "kirby2/release/diagnostics.py",
            _repository_root() / "kirby2/release/doctor.py",
        ):
            tree = ast.parse(path.read_text("utf-8"), filename=path.as_posix())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_roots.update(
                        alias.name.partition(".")[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots.add(node.module.partition(".")[0])
        forbidden_network_imports = {
            "aiohttp",
            "http",
            "requests",
            "socket",
            "urllib",
        }
        observed_forbidden = sorted(imported_roots & forbidden_network_imports)
        if observed_forbidden:
            command_failures.append(
                "release support source imports network clients: "
                + ",".join(observed_forbidden)
            )
        command_case = _case(
            "release_support_commands_and_offline_boundary_are_explicit",
            "The five named support commands are unique and no service/updater seam is introduced.",
            {"required_commands": list(required_commands)},
            command_failures,
        )

    cases = (first_run_case, diagnostic_case, command_case)
    if len(cases) != WO40C_AUDIT_CASE_COUNT:
        raise RuntimeError("WO40-C release audit inventory changed")
    return ReleaseAuditSuite("WO40-C", cases)


def _required_future_check_ids(gate_id: str) -> tuple[str, ...]:
    from kirby2.release.performance import RELEASE_PERFORMANCE_CELL_ORDER_V1
    from kirby2.release.qualification import (
        RELEASE_FUNCTIONAL_STEP_ORDER_V1,
        RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1,
    )

    if gate_id == "WO40-F":
        return (
            "CANDIDATE_SOURCE_LOCK",
            "HEADLESS_ARTIFACTS",
            "DESKTOP_ARTIFACTS",
            "REPEAT_BUILD_REPRODUCIBILITY",
            "MANIFEST_LICENSE_PACK_ASSET_INVENTORY",
            "OFFLINE_INSTALLABILITY",
            "NO_DEVELOPER_DATA",
        )
    if gate_id in {"WO40-G", "WO40-H"}:
        platform_check = (
            "CROSS_PLATFORM_INTEGER_CORE_BASELINE"
            if gate_id == "WO40-G"
            else "CROSS_PLATFORM_INTEGER_CORE_MATCH"
        )
        return (
            "CLEAN_PROVIDER",
            "INSTALLED_ARTIFACT_ONLY",
            *(f"DESKTOP:{step}" for step in RELEASE_FUNCTIONAL_STEP_ORDER_V1),
            *(f"HEADLESS:{step}" for step in RELEASE_FUNCTIONAL_STEP_ORDER_V1),
            *(f"HEADLESS:{step}" for step in RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1),
            "SAME_PLATFORM_DESKTOP_HEADLESS",
            platform_check,
        )
    if gate_id == "WO40-I":
        return (
            "CANDIDATE_SOURCE_LOCK",
            "IMMUTABLE_ARTIFACT_BINDING",
            *(f"CELL:{item.value}" for item in RELEASE_PERFORMANCE_CELL_ORDER_V1),
            "AUXILIARY:RELEASE_INTERACTIVE_ACK_V1",
            "AUXILIARY:RELEASE_TERMINAL_UPDATE_V1",
            "AUXILIARY:RELEASE_FULL_DAY_GENERATION_V1",
            "AUXILIARY:RELEASE_FULL_DAY_REPLAY_V1",
            "AUXILIARY:RELEASE_MICROSCOPE_LOAD_V1",
            "EXACT_10000_COMPLETE_WORK_UNITS",
            "UNIQUE_COMPLETE_RUN_LOGICAL_IDS",
            "FULL_RESULT_ARTIFACT_AUDIT_RECORDS",
            "DETERMINISTIC_AGGREGATE",
            "PREREGISTERED_ABORT_RETRY_POLICY",
        )
    raise ValueError(f"unknown future release evidence gate: {gate_id}")


@dataclass(frozen=True, slots=True)
class ReleaseGateEvidenceV1:
    gate_id: str
    status: str
    candidate_commit: str
    protocol_set_sha256: str
    source_manifest_sha256: str
    artifact_index_sha256: str
    checks: tuple[dict[str, object], ...]
    evidence_records: tuple[dict[str, object], ...]
    facts: dict[str, object]

    schema_id: str = RELEASE_GATE_EVIDENCE_SCHEMA_ID_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.gate_id) is not str
            or type(self.status) is not str
            or type(self.candidate_commit) is not str
            or type(self.protocol_set_sha256) is not str
            or type(self.source_manifest_sha256) is not str
            or type(self.artifact_index_sha256) is not str
            or type(self.schema_id) is not str
            or type(self.schema_version) is not int
        ):
            raise TypeError("release evidence identity fields have invalid types")
        if type(self.checks) is not tuple or type(self.evidence_records) is not tuple:
            raise TypeError("release evidence collections must be immutable tuples")
        if self.gate_id not in RELEASE_FUTURE_EVIDENCE_PATHS_V1:
            raise ValueError("release evidence gate is not preregistered")
        if self.schema_id != RELEASE_GATE_EVIDENCE_SCHEMA_ID_V1 or self.schema_version != 1:
            raise ValueError("release evidence schema identity differs")
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("release evidence candidate commit is invalid")
        for value, label in (
            (self.protocol_set_sha256, "protocol set"),
            (self.source_manifest_sha256, "source manifest"),
            (self.artifact_index_sha256, "artifact index"),
        ):
            if _SHA256.fullmatch(value) is None:
                raise ValueError(f"release evidence {label} digest is invalid")

        required = _required_future_check_ids(self.gate_id)
        check_ids: list[str] = []
        observed_statuses: list[str] = []
        for raw in self.checks:
            if type(raw) is not dict or set(raw) != {
                "check_id",
                "evidence_sha256",
                "status",
            }:
                raise ValueError("release evidence check fields differ")
            check_id = raw["check_id"]
            check_status = raw["status"]
            check_digest = raw["evidence_sha256"]
            if type(check_id) is not str or _TOKEN.fullmatch(check_id) is None:
                raise ValueError("release evidence check ID is invalid")
            if check_status not in {"PASS", "WARNING", "FAIL", "NOT_EXERCISED"}:
                raise ValueError("release evidence check status is invalid")
            if type(check_digest) is not str or _SHA256.fullmatch(check_digest) is None:
                raise ValueError("release evidence check digest is invalid")
            check_ids.append(check_id)
            observed_statuses.append(check_status)
        if tuple(check_ids) != required:
            raise ValueError("release evidence check inventory or order differs")
        expected_status = (
            "FAIL"
            if "FAIL" in observed_statuses
            else (
                "NOT_EXERCISED"
                if "NOT_EXERCISED" in observed_statuses
                else (
                    "PASS_WITH_WARNINGS"
                    if "WARNING" in observed_statuses
                    else "PASS"
                )
            )
        )
        if self.status != expected_status:
            raise ValueError("release evidence aggregate status does not reconcile")

        evidence_ids: list[str] = []
        evidence_paths: list[str] = []
        if not self.evidence_records:
            raise ValueError("release evidence requires immutable referenced records")
        for raw in self.evidence_records:
            if type(raw) is not dict or set(raw) != {
                "evidence_id",
                "path",
                "sha256",
                "size",
            }:
                raise ValueError("release evidence record fields differ")
            evidence_id = raw["evidence_id"]
            relative_path = raw["path"]
            size = raw["size"]
            digest = raw["sha256"]
            if type(evidence_id) is not str or _TOKEN.fullmatch(evidence_id) is None:
                raise ValueError("release evidence record ID is invalid")
            if type(relative_path) is not str:
                raise TypeError("release evidence record path must be text")
            relative = Path(relative_path)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or relative.parts[:2] != (".kirby2", "release")
            ):
                raise ValueError("release evidence record path is outside the artifact store")
            if type(size) is not int or size <= 0:
                raise ValueError("release evidence record size must be positive")
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                raise ValueError("release evidence record digest is invalid")
            evidence_ids.append(evidence_id)
            evidence_paths.append(relative.as_posix())
        if evidence_ids != sorted(evidence_ids, key=lambda item: item.encode("utf-8")):
            raise ValueError("release evidence records must be ID-sorted")
        if len(evidence_ids) != len(set(evidence_ids)) or len(evidence_paths) != len(
            set(evidence_paths)
        ):
            raise ValueError("release evidence records must be unique")
        self._validate_facts()

    def _validate_facts(self) -> None:
        if type(self.facts) is not dict:
            raise TypeError("release evidence facts must be an object")
        if self.gate_id == "WO40-F":
            if (
                set(self.facts) != {"artifact_count", "build_repetitions"}
                or type(self.facts["artifact_count"]) is not int
                or type(self.facts["build_repetitions"]) is not int
                or self.facts != {"artifact_count": 6, "build_repetitions": 2}
            ):
                raise ValueError("WO40-F evidence facts differ")
            return
        if self.gate_id in {"WO40-G", "WO40-H"}:
            expected = {
                "clean_environment",
                "cross_platform_integer_core_sha256",
                "desktop_run_sha256",
                "headless_run_sha256",
                "platform_id",
                "replay_sha256",
            }
            if set(self.facts) != expected:
                raise ValueError("platform evidence fact fields differ")
            target = "macos-arm64" if self.gate_id == "WO40-G" else "linux-x86_64"
            if self.facts["clean_environment"] is not True or self.facts["platform_id"] != target:
                raise ValueError("platform evidence target or clean-root claim differs")
            for field in (
                "cross_platform_integer_core_sha256",
                "desktop_run_sha256",
                "headless_run_sha256",
                "replay_sha256",
            ):
                value = self.facts[field]
                if type(value) is not str or _SHA256.fullmatch(value) is None:
                    raise ValueError(f"platform evidence {field} is invalid")
            if self.facts["desktop_run_sha256"] != self.facts["headless_run_sha256"]:
                raise ValueError("same-platform desktop/headless run identities differ")
            return
        expected_facts = {
            "auxiliary_result_count": 5,
            "complete_artifact_records": 10_000,
            "complete_audit_records": 10_000,
            "complete_result_records": 10_000,
            "complete_run_work_units": 10_000,
            "unique_complete_run_ids": 10_000,
        }
        if (
            set(self.facts) != set(expected_facts)
            or any(type(value) is not int for value in self.facts.values())
            or self.facts != expected_facts
        ):
            raise ValueError("WO40-I evidence facts differ")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_index_sha256": self.artifact_index_sha256,
            "candidate_commit": self.candidate_commit,
            "checks": list(self.checks),
            "evidence_records": list(self.evidence_records),
            "facts": self.facts,
            "gate_id": self.gate_id,
            "protocol_set_sha256": self.protocol_set_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_manifest_sha256": self.source_manifest_sha256,
            "status": self.status,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ReleaseGateEvidenceV1:
        if type(value) is not dict or set(value) != {
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
        }:
            raise ValueError("release gate evidence fields differ")
        checks = value["checks"]
        evidence_records = value["evidence_records"]
        facts = value["facts"]
        if type(checks) is not list or type(evidence_records) is not list or type(facts) is not dict:
            raise TypeError("release gate evidence collections differ")
        text_fields = (
            "artifact_index_sha256",
            "candidate_commit",
            "gate_id",
            "protocol_set_sha256",
            "schema_id",
            "source_manifest_sha256",
            "status",
        )
        if any(type(value[field]) is not str for field in text_fields):
            raise TypeError("release gate evidence identity fields must be text")
        return cls(
            artifact_index_sha256=value["artifact_index_sha256"],
            candidate_commit=value["candidate_commit"],
            checks=tuple(dict(item) if type(item) is dict else item for item in checks),
            evidence_records=tuple(
                dict(item) if type(item) is dict else item for item in evidence_records
            ),
            facts=dict(facts),
            gate_id=value["gate_id"],
            protocol_set_sha256=value["protocol_set_sha256"],
            schema_id=value["schema_id"],
            schema_version=value["schema_version"],
            source_manifest_sha256=value["source_manifest_sha256"],
            status=value["status"],
        )


def render_release_gate_evidence_markdown(
    evidence: ReleaseGateEvidenceV1,
) -> str:
    if type(evidence) is not ReleaseGateEvidenceV1:
        raise TypeError("release evidence renderer requires ReleaseGateEvidenceV1")
    return (
        f"# Kirby2 {evidence.gate_id} Release Evidence\n\n"
        f"Status: `{evidence.status}`\n\n"
        "This document references immutable release-store records; the embedded "
        "canonical payload is the audited contract.\n\n"
        + RELEASE_EVIDENCE_MARKER_START_V1
        + evidence.canonical_bytes().decode("ascii")
        + RELEASE_EVIDENCE_MARKER_END_V1
        + "\n"
    )


def parse_release_gate_evidence_markdown(raw: bytes) -> ReleaseGateEvidenceV1:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("release evidence document is not UTF-8") from error
    start = text.find(RELEASE_EVIDENCE_MARKER_START_V1)
    end = text.find(
        RELEASE_EVIDENCE_MARKER_END_V1,
        start + len(RELEASE_EVIDENCE_MARKER_START_V1),
    )
    if start < 0 or end < 0:
        raise ValueError("release evidence canonical marker is missing")
    if text.find(RELEASE_EVIDENCE_MARKER_START_V1, start + 1) >= 0:
        raise ValueError("release evidence contains duplicate canonical markers")
    payload_raw = text[
        start + len(RELEASE_EVIDENCE_MARKER_START_V1) : end
    ].encode("ascii")
    payload = load_canonical_json_bytes(payload_raw, "release gate evidence")
    restored = ReleaseGateEvidenceV1.from_dict(payload)
    if restored.canonical_bytes() != payload_raw:
        raise ValueError("release evidence payload is not canonical")
    return restored


def _synthetic_future_evidence(gate_id: str) -> ReleaseGateEvidenceV1:
    checks = tuple(
        {
            "check_id": check_id,
            "evidence_sha256": _sha256(check_id.encode("utf-8")),
            "status": "PASS",
        }
        for check_id in _required_future_check_ids(gate_id)
    )
    if gate_id == "WO40-F":
        facts: dict[str, object] = {"artifact_count": 6, "build_repetitions": 2}
    elif gate_id in {"WO40-G", "WO40-H"}:
        facts = {
            "clean_environment": True,
            "cross_platform_integer_core_sha256": "5" * 64,
            "desktop_run_sha256": "6" * 64,
            "headless_run_sha256": "6" * 64,
            "platform_id": "macos-arm64" if gate_id == "WO40-G" else "linux-x86_64",
            "replay_sha256": "7" * 64,
        }
    else:
        facts = {
            "auxiliary_result_count": 5,
            "complete_artifact_records": 10_000,
            "complete_audit_records": 10_000,
            "complete_result_records": 10_000,
            "complete_run_work_units": 10_000,
            "unique_complete_run_ids": 10_000,
        }
    return ReleaseGateEvidenceV1(
        gate_id=gate_id,
        status="PASS",
        candidate_commit="1" * 40,
        protocol_set_sha256="2" * 64,
        source_manifest_sha256="3" * 64,
        artifact_index_sha256="4" * 64,
        checks=checks,
        evidence_records=(
            {
                "evidence_id": "synthetic-record",
                "path": ".kirby2/release/synthetic-record.json",
                "sha256": "8" * 64,
                "size": 1,
            },
        ),
        facts=facts,
    )


def audit_release_protocol() -> ReleaseAuditSuite:
    """Audit preregistered protocols, dispatch refusal, and future evidence bytes."""

    from kirby2.release.build import load_release_protocol_bundle
    from kirby2.release.performance import (
        RELEASE_PERFORMANCE_CELL_ORDER_V1,
        RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1,
        RunnerSourceTreeV1,
        bind_performance_row_template,
        build_performance_row_template,
    )
    from kirby2.release.qualification import (
        RELEASE_FUNCTIONAL_STEP_ORDER_V1,
        RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1,
        ReleaseQualificationRefusalCodeV1,
        ReleaseQualificationRefused,
        qualification_dispatch,
    )

    repository = _repository_root()
    bundle = load_release_protocol_bundle(repository)
    protocol_failures: list[str] = []
    targets = tuple(item.target_id for item in bundle.platform_protocol.targets)
    if targets != ("macos-arm64", "linux-x86_64"):
        protocol_failures.append("minimum release platform matrix differs")
    if bundle.performance_protocol.row_count != 10_000:
        protocol_failures.append("performance protocol is not exactly 10,000 work units")
    if len(bundle.artifact_layout.artifacts) != 6:
        protocol_failures.append("release artifact inventory is not exactly six transports")
    if tuple(
        item.step_id for item in bundle.qualification_protocol.functional_steps
    ) != RELEASE_FUNCTIONAL_STEP_ORDER_V1:
        protocol_failures.append("functional qualification matrix differs")
    if tuple(
        item.step_id for item in bundle.qualification_protocol.headless_extra_steps
    ) != RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1:
        protocol_failures.append("headless qualification matrix differs")
    protocol_case = _case(
        "complete_release_protocol_bundle_is_digest_bound",
        "Targets, six artifacts, functional rows, and 10,000-unit protocol parse together.",
        {
            "artifact_count": len(bundle.artifact_layout.artifacts),
            "protocol_set_sha256": bundle.protocol_set_sha256,
            "row_count": bundle.performance_protocol.row_count,
            "targets": list(targets),
        },
        protocol_failures,
    )

    dispatch_failures: list[str] = []
    dispatch_rows: list[tuple[str, int]] = []
    for target in targets:
        for form in ("desktop", "headless"):
            dispatch = qualification_dispatch(
                bundle.qualification_protocol,
                target_id=target,
                artifact_selector=f"{target}/{form}",
                clean_provider_id=f"synthetic-{target}-provider",
                clean_root_role="PRIMARY_CLEAN_ROOT",
                prior_attempt_exists=False,
            )
            expected_count = len(RELEASE_FUNCTIONAL_STEP_ORDER_V1) + (
                len(RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1) if form == "headless" else 0
            )
            if dispatch.status.value != "READY" or len(dispatch.step_ids) != expected_count:
                dispatch_failures.append(f"{target}/{form} dispatch differs")
            dispatch_rows.append((f"{target}/{form}", len(dispatch.step_ids)))
    try:
        qualification_dispatch(
            bundle.qualification_protocol,
            target_id="macos-arm64",
            artifact_selector="macos-arm64/desktop",
            clean_provider_id=None,
            clean_root_role="PRIMARY_CLEAN_ROOT",
            prior_attempt_exists=False,
        )
    except ReleaseQualificationRefused as error:
        if error.code is not ReleaseQualificationRefusalCodeV1.CLEAN_PROVIDER_MISSING:
            dispatch_failures.append("missing provider used the wrong refusal code")
    else:
        dispatch_failures.append("qualification dispatched without a clean provider")
    dispatch_case = _case(
        "qualification_dispatch_is_exact_and_nonexecuting",
        "Both target/form matrices resolve exactly and missing clean providers refuse.",
        {"dispatches": [[key, count] for key, count in dispatch_rows]},
        dispatch_failures,
    )

    source_tree = RunnerSourceTreeV1.from_bytes(
        (repository / "release/performance_runner_sources.lock").read_bytes()
    )
    first = build_performance_row_template(
        RELEASE_PERFORMANCE_CELL_ORDER_V1[0], 4_000_000
    )
    last = build_performance_row_template(
        RELEASE_PERFORMANCE_CELL_ORDER_V1[-1], 4_000_999
    )
    bound_first = bind_performance_row_template(first, source_tree)
    bound_last = bind_performance_row_template(last, source_tree)
    row_failures: list[str] = []
    if RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1 != 10_000:
        row_failures.append("performance work-unit constant differs")
    if bound_first["runner_source_sha256"] != source_tree.source_manifest_sha256:
        row_failures.append("first performance row is not source-lock bound")
    if bound_last["runner_source_sha256"] != source_tree.source_manifest_sha256:
        row_failures.append("last performance row is not source-lock bound")
    row_case = _case(
        "performance_frontier_binds_first_and_last_rows",
        "The fixed 10,000-row corpus binds both extrema to one runner source tree.",
        {
            "first_work_unit_id": first.work_unit_id,
            "last_work_unit_id": last.work_unit_id,
            "row_corpus_sha256": bundle.performance_protocol.row_corpus_sha256,
        },
        row_failures,
    )

    evidence_failures: list[str] = []
    evidence_digests: list[list[str]] = []
    for gate_id in RELEASE_FUTURE_EVIDENCE_PATHS_V1:
        synthetic = _synthetic_future_evidence(gate_id)
        raw = render_release_gate_evidence_markdown(synthetic).encode("utf-8")
        restored = parse_release_gate_evidence_markdown(raw)
        if restored != synthetic:
            evidence_failures.append(f"{gate_id} evidence did not round-trip")
        evidence_digests.append([gate_id, _sha256(restored.canonical_bytes())])
        tampered = bytearray(restored.canonical_bytes())
        tampered[-1] = ord(" ")
        hostile = (
            RELEASE_EVIDENCE_MARKER_START_V1.encode("ascii")
            + bytes(tampered)
            + RELEASE_EVIDENCE_MARKER_END_V1.encode("ascii")
        )
        try:
            parse_release_gate_evidence_markdown(hostile)
        except (TypeError, ValueError):
            pass
        else:
            evidence_failures.append(f"{gate_id} accepted noncanonical evidence")
    evidence_case = _case(
        "future_evidence_contract_is_strict_and_preregistered",
        "F-I evidence schemas accept exact fixtures and reject altered canonical bytes.",
        {"evidence_payloads": evidence_digests},
        evidence_failures,
    )

    cases = (protocol_case, dispatch_case, row_case, evidence_case)
    if len(cases) != WO40D_AUDIT_CASE_COUNT:
        raise RuntimeError("WO40-D release audit inventory changed")
    return ReleaseAuditSuite("WO40-D", cases)


def audit_release_resource_report() -> ReleaseAuditSuite:
    """Compare the committed D1 report with a fresh read-only resource preflight."""

    from kirby2.release.build import (
        load_release_protocol_bundle,
        release_resource_preflight,
    )

    repository = _repository_root()
    bundle = load_release_protocol_bundle(repository)
    provider_inventory = repository / ".kirby2/release/clean-providers.toml"
    live = release_resource_preflight(
        bundle,
        wheelhouse_root=repository / "release/wheelhouse",
        provider_inventory=(provider_inventory if provider_inventory.is_file() else None),
    )
    live_failures = tuple(
        f"{item.resource_id}: {item.detail}" for item in live.missing_items
    )
    live_case = _case(
        "offline_resources_and_clean_providers_are_ready",
        "Both target wheelhouses and real clean-environment providers pass read-only preflight.",
        {
            "missing_item_count": len(live.missing_items),
            "protocol_commit": live.protocol_commit,
            "protocol_set_sha256": live.protocol_set_sha256,
            "status": live.status,
        },
        live_failures,
    )

    report_path = repository / "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md"
    expected = live.markdown().encode("utf-8")
    report_failures: list[str] = []
    if not report_path.is_file():
        report_failures.append("tracked release resource preflight report is missing")
        actual = b""
    else:
        actual = report_path.read_bytes()
        if actual != expected:
            report_failures.append("tracked preflight report is stale or not byte-exact")
    report_case = _case(
        "tracked_preflight_report_matches_live_protocol",
        "The committed Markdown report is the exact current no-network preflight rendering.",
        {
            "expected_sha256": _sha256(expected),
            "observed_sha256": _sha256(actual),
        },
        report_failures,
    )
    cases = (live_case, report_case)
    if len(cases) != WO40D1_AUDIT_CASE_COUNT:
        raise RuntimeError("WO40-D1 release audit inventory changed")
    return ReleaseAuditSuite("WO40-D1", cases)


def audit_release_preflight_provenance() -> ReleaseAuditSuite:
    """Prove D1 binds protocol history rather than its own evidence commit."""

    from kirby2.release.build import (
        load_release_protocol_bundle,
        release_resource_preflight,
    )
    from kirby2.release.manifest import RELEASE_PROTOCOL_PATHS_V1

    repository = _repository_root()
    bundle = load_release_protocol_bundle(repository)
    provider_inventory = repository / ".kirby2/release/clean-providers.toml"
    preflight = release_resource_preflight(
        bundle,
        wheelhouse_root=repository / "release/wheelhouse",
        provider_inventory=(
            provider_inventory if provider_inventory.is_file() else None
        ),
    )
    history = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--first-parent",
            "--format=%H",
            "--",
            *RELEASE_PROTOCOL_PATHS_V1,
        ],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    history_commit = history.stdout.decode("ascii", errors="replace").strip()
    head_commit = head.stdout.decode("ascii", errors="replace").strip()
    provenance_failures: list[str] = []
    if history.returncode != 0 or _COMMIT.fullmatch(history_commit) is None:
        provenance_failures.append("protocol path history did not resolve a commit")
    if preflight.protocol_commit != history_commit:
        provenance_failures.append("preflight does not bind the protocol-owning revision")
    if preflight.status != "PASS":
        provenance_failures.append("stable protocol provenance did not pass resource preflight")
    provenance_case = _case(
        "protocol_revision_is_owned_by_frozen_input_history",
        "The resolved revision is the newest first-parent commit touching an exact protocol path.",
        {
            "protocol_commit": preflight.protocol_commit,
            "protocol_path_count": len(RELEASE_PROTOCOL_PATHS_V1),
            "protocol_set_sha256": preflight.protocol_set_sha256,
        },
        provenance_failures,
    )

    report_path = repository / "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md"
    expected = preflight.markdown().encode("utf-8")
    observed = report_path.read_bytes() if report_path.is_file() else b""
    stability_failures: list[str] = []
    if head.returncode != 0 or _COMMIT.fullmatch(head_commit) is None:
        stability_failures.append("repository HEAD did not resolve")
    elif head_commit == preflight.protocol_commit:
        stability_failures.append("audit fixture lacks a later nonprotocol evidence commit")
    if observed != expected:
        stability_failures.append("preflight report is not exact after a nonprotocol commit")
    stability_case = _case(
        "nonprotocol_commits_do_not_invalidate_resource_evidence",
        "A later report/audit commit leaves the protocol revision and exact D1 bytes stable.",
        {
            "head_commit": head_commit,
            "report_sha256": _sha256(observed),
            "resolved_protocol_commit": preflight.protocol_commit,
        },
        stability_failures,
    )
    cases = (provenance_case, stability_case)
    if len(cases) != DEV0010_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0010 release audit inventory changed")
    return ReleaseAuditSuite(
        "DEV-0010",
        cases,
        metadata=(("interrupted_card", "WO40-E"),),
    )


def _capture_release_surface(entrypoint, arguments: tuple[str, ...]) -> tuple[int, bytes]:
    stream = io.StringIO()
    exit_code = 0
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        try:
            result = entrypoint(arguments)
        except SystemExit as error:
            exit_code = 0 if error.code is None else int(error.code)
        else:
            exit_code = 0 if result is None else int(result)
    return exit_code, stream.getvalue().encode("utf-8")


def _indexed_runner_source_tree(repository: Path):
    from kirby2.release.performance import RunnerSourceEntryV1, RunnerSourceTreeV1

    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "kirby2",
            "pyproject.toml",
        ],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        raise RuntimeError("candidate source status could not be inspected")
    rows = status.stdout.splitlines()
    if any(row[:2] == b"??" for row in rows):
        raise ValueError("candidate source projection contains an untracked file")
    unstaged = subprocess.run(
        ["git", "diff", "--quiet", "--", "kirby2", "pyproject.toml"],
        cwd=repository,
        check=False,
    )
    if unstaged.returncode != 0:
        raise ValueError("candidate source projection contains unstaged bytes")
    listed = subprocess.run(
        ["git", "ls-files", "-s", "-z", "--", "kirby2", "pyproject.toml"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError("candidate source index could not be read")
    paths: list[str] = []
    for record in listed.stdout.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise ValueError("candidate source index record is malformed")
        mode, _object_id, stage = fields
        if stage != b"0":
            raise ValueError("candidate source index contains an unresolved stage")
        if mode not in {b"100644", b"100755"}:
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("candidate source path is not UTF-8") from error
        paths.append(path)
    ordered = tuple(sorted(paths, key=lambda item: item.encode("utf-8")))
    if tuple(paths) != ordered:
        raise ValueError("Git source projection is not path sorted")
    entries = tuple(
        RunnerSourceEntryV1(
            path=path,
            sha256=_sha256((repository / path).read_bytes()),
        )
        for path in ordered
    )
    manifest_digest = _sha256(
        canonical_json_bytes([item.as_dict() for item in entries])
    )
    return RunnerSourceTreeV1(
        source_manifest=entries,
        source_manifest_sha256=manifest_digest,
    )


def audit_release_candidate_source() -> ReleaseAuditSuite:
    """Audit the equivalent release surfaces, docs, launchers, and source lock."""

    from kirby2.release.desktop import main as desktop_main
    from kirby2.release.headless import RELEASE_BOUNDARIES_V1, main as headless_main
    from kirby2.release.performance import RunnerSourceTreeV1

    repository = _repository_root()
    headless_exit, headless_output = _capture_release_surface(
        headless_main,
        ("demo", "--seed", "42"),
    )
    desktop_exit, desktop_output = _capture_release_surface(
        desktop_main,
        ("cli", "demo", "--seed", "42"),
    )
    equivalence_failures: list[str] = []
    if (headless_exit, headless_output) != (desktop_exit, desktop_output):
        equivalence_failures.append("desktop/headless canonical demo outputs differ")
    if headless_exit != 0:
        equivalence_failures.append("canonical release demo did not exit successfully")
    equivalence_case = _case(
        "desktop_and_headless_delegate_to_one_runtime",
        "Identical canonical demo inputs produce byte-identical release-surface output.",
        {
            "exit_code": headless_exit,
            "output_sha256": _sha256(headless_output),
        },
        equivalence_failures,
    )

    launcher_paths = (
        "release/launchers/headless/kirby2",
        "release/launchers/linux/kirby2",
        "release/launchers/macos/kirby2",
    )
    launcher_failures: list[str] = []
    launcher_digests: list[list[str]] = []
    for relative in launcher_paths:
        path = repository / relative
        if not path.is_file():
            launcher_failures.append(f"missing launcher {relative}")
            continue
        raw = path.read_bytes()
        launcher_digests.append([relative, _sha256(raw)])
        if not stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR:
            launcher_failures.append(f"launcher is not executable: {relative}")
        lowered = raw.lower()
        if any(token in lowered for token in (b"curl ", b"wget ", b"http://", b"https://")):
            launcher_failures.append(f"launcher introduces a network seam: {relative}")
    launcher_case = _case(
        "platform_launchers_are_local_offline_delegates",
        "All three executable launchers resolve installed local entrypoints without networking.",
        {"launchers": launcher_digests},
        launcher_failures,
    )

    documentation_paths = (
        "docs/INSTRUCTOR_RESEARCH.md",
        "docs/LIMITATIONS.md",
        "docs/SCENARIO_AUTHORING.md",
        "docs/SECURITY_PRIVACY.md",
        "docs/TROUBLESHOOTING.md",
        "docs/USER_GUIDE.md",
    )
    documentation_failures: list[str] = []
    documentation_digests: list[list[str]] = []
    normalized_boundaries = tuple(item.casefold() for item in RELEASE_BOUNDARIES_V1)
    for relative in documentation_paths:
        path = repository / relative
        if not path.is_file():
            documentation_failures.append(f"missing user documentation {relative}")
            continue
        raw = path.read_bytes()
        documentation_digests.append([relative, _sha256(raw)])
        text = raw.decode("utf-8").casefold()
        if any(boundary not in text for boundary in normalized_boundaries):
            documentation_failures.append(f"five release boundaries are incomplete in {relative}")
    documentation_case = _case(
        "user_documentation_states_all_five_boundaries",
        "Every release document states simulation, broker, connector, guarantee, and history limits.",
        {"documents": documentation_digests},
        documentation_failures,
    )

    source_lock_path = repository / "release/performance_runner_sources.lock"
    source_failures: list[str] = []
    observed_digest = "0" * 64
    expected_digest = "0" * 64
    try:
        observed = RunnerSourceTreeV1.from_bytes(source_lock_path.read_bytes())
        expected = _indexed_runner_source_tree(repository)
        observed_digest = observed.source_manifest_sha256
        expected_digest = expected.source_manifest_sha256
        if observed != expected:
            source_failures.append("runner source lock differs from the fully staged source projection")
        if source_lock_path.read_bytes() != canonical_json_bytes(observed.as_dict()):
            source_failures.append("runner source lock bytes are not compact canonical JSON")
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        source_failures.append(f"runner source lock validation failed: {type(error).__name__}")
    source_case = _case(
        "runner_source_lock_matches_the_git_index",
        "The exhaustive canonical lock matches every staged regular blob under kirby2 plus pyproject.",
        {"expected_sha256": expected_digest, "observed_sha256": observed_digest},
        source_failures,
    )

    cases = (equivalence_case, launcher_case, documentation_case, source_case)
    if len(cases) != WO40E_AUDIT_CASE_COUNT:
        raise RuntimeError("WO40-E release audit inventory changed")
    return ReleaseAuditSuite("WO40-E", cases)


def audit_release_candidate_input_restart() -> ReleaseAuditSuite:
    """Prove the restarted WO40-F planner binds real immutable candidate inputs."""

    import kirby2.release.build as release_build_module

    from kirby2.release.build import (
        ReleaseBuildRefusalCodeV1,
        ReleaseCommandStatusV1,
        load_release_protocol_bundle,
        plan_release_build,
        release_resource_preflight,
    )
    from kirby2.release.manifest import RELEASE_PROTOCOL_PATHS_V1

    clean_failures: list[str] = []
    identity_failures: list[str] = []
    dirty_failures: list[str] = []
    source_failures: list[str] = []
    protocol_failures: list[str] = []
    clean_evidence: dict[str, object] = {
        "artifact_output_created": False,
        "candidate_commit": _DEV0011_PREDECESSOR_COMMIT_V1,
        "resource_drift_refusal_code": None,
        "source_entry_count": 0,
        "source_manifest_sha256": "0" * 64,
    }
    identity_evidence: dict[str, object] = {
        "non_head_refusal_code": None,
        "replacement_refusal_code": None,
        "unresolved_refusal_code": None,
    }
    dirty_evidence: dict[str, object] = {
        "assume_unchanged_refusal_code": None,
        "skip_worktree_refusal_code": None,
        "staged_refusal_code": None,
        "untracked_refusal_code": None,
        "unstaged_refusal_code": None,
    }
    source_evidence: dict[str, object] = {"refusal_code": None}
    protocol_evidence: dict[str, object] = {"protocols": []}
    fixture_candidate = _DEV0011_PREDECESSOR_COMMIT_V1
    source_repository = _repository_root()

    def run_git(repository: Path, *arguments: str) -> bytes:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"temporary Git command failed: {arguments[0]}")
        return result.stdout

    def reset_fixture(repository: Path) -> None:
        run_git(
            repository,
            "checkout",
            "--quiet",
            "--force",
            "--detach",
            fixture_candidate,
        )

    def commit_fixture(repository: Path, relative: str, subject: str) -> str:
        run_git(repository, "add", "--", relative)
        environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000",
        }
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Kirby2 Release Audit",
                "-c",
                "user.email=release-audit.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "--no-verify",
                "-m",
                subject,
            ],
            cwd=repository,
            env=environment,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("temporary candidate commit failed")
        return run_git(repository, "rev-parse", "--verify", "HEAD^{commit}").decode(
            "ascii"
        ).strip()

    @contextlib.contextmanager
    def activate_fixture_runtime(
        repository: Path,
        source_repository: Path,
    ) -> Iterable[None]:
        """Point runtime discovery at the hardlinked detached-fixture venv."""

        runtime_sys = release_build_module.sys
        runtime_site = release_build_module.site
        runtime_sysconfig = release_build_module.sysconfig
        source_site_packages = (
            source_repository / ".venv/lib/python3.14/site-packages"
        ).resolve(strict=True)
        fixture_site_packages = (
            repository / ".venv/lib/python3.14/site-packages"
        ).resolve(strict=True)
        original_prefix = runtime_sys.prefix
        original_executable = runtime_sys.executable
        original_path = tuple(runtime_sys.path)
        original_getsitepackages = runtime_site.getsitepackages
        original_get_path = runtime_sysconfig.get_path

        def fixture_getsitepackages(prefixes: object = None) -> list[str]:
            del prefixes
            return [os.fspath(fixture_site_packages)]

        def fixture_get_path(name: str, *args: object, **kwargs: object) -> str:
            if name in {"purelib", "platlib"}:
                return os.fspath(fixture_site_packages)
            return original_get_path(name, *args, **kwargs)

        fixture_path: list[str] = []
        for value in original_path:
            try:
                resolved = Path(value).resolve(strict=False)
            except (OSError, TypeError, ValueError):
                fixture_path.append(value)
                continue
            fixture_path.append(
                os.fspath(fixture_site_packages)
                if resolved == source_site_packages
                else value
            )
        runtime_sys.prefix = os.fspath(repository / ".venv")
        runtime_sys.executable = os.fspath(repository / ".venv/bin/python")
        runtime_sys.path[:] = fixture_path
        runtime_site.getsitepackages = fixture_getsitepackages
        runtime_sysconfig.get_path = fixture_get_path
        try:
            yield
        finally:
            runtime_sys.prefix = original_prefix
            runtime_sys.executable = original_executable
            runtime_sys.path[:] = original_path
            runtime_site.getsitepackages = original_getsitepackages
            runtime_sysconfig.get_path = original_get_path

    with (
        TemporaryDirectory(
            prefix="kirby2-release-dev0011-",
            dir=source_repository / ".kirby2",
        ) as temporary,
        contextlib.ExitStack() as fixture_stack,
    ):
        temporary_root = Path(temporary).resolve()
        fixture = temporary_root / "candidate"
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                os.fspath(_repository_root()),
                os.fspath(fixture),
            ],
            capture_output=True,
            check=False,
        )
        setup_error: str | None = None
        try:
            if clone.returncode != 0:
                raise RuntimeError("temporary local clone failed")
            reset_fixture(fixture)
            (fixture / "UNTRACKED_DEV0011_SENTINEL.txt").write_text(
                "untracked non-input bytes must not alter a Git-object build plan\n",
                encoding="utf-8",
            )
            shutil.copytree(
                source_repository / ".venv",
                fixture / ".venv",
                copy_function=os.link,
                symlinks=True,
            )
            fixture_stack.enter_context(
                activate_fixture_runtime(fixture, source_repository)
            )
            wheel_sources = tuple(
                sorted(
                    (source_repository / "release/wheelhouse").rglob("*.whl"),
                    key=lambda path: os.fspath(path).encode("utf-8"),
                )
            )
            if len(wheel_sources) != 2:
                raise RuntimeError("release audit requires both frozen wheel inputs")
            resource_sources = (
                source_repository / ".kirby2/release/clean-providers.toml",
                *wheel_sources,
            )
            for source in resource_sources:
                relative = source.relative_to(source_repository)
                target = fixture / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
            fixture_bundle = load_release_protocol_bundle(fixture)
            fixture_preflight = release_resource_preflight(
                fixture_bundle,
                wheelhouse_root=fixture / "release/wheelhouse",
                provider_inventory=fixture / ".kirby2/release/clean-providers.toml",
            )
            (fixture / "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md").write_text(
                fixture_preflight.markdown(),
                encoding="utf-8",
            )
            fixture_candidate = commit_fixture(
                fixture,
                "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md",
                "Refresh historical resource report fixture",
            )
            reset_fixture(fixture)
        except (OSError, RuntimeError, UnicodeError) as error:
            setup_error = f"candidate fixture setup failed: {type(error).__name__}"

        if setup_error is not None:
            for failures in (
                clean_failures,
                identity_failures,
                dirty_failures,
                source_failures,
                protocol_failures,
            ):
                failures.append(setup_error)
        else:
            output_root = fixture / "artifact-output"
            try:
                bundle = load_release_protocol_bundle(fixture)
                outcome = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                inputs = outcome.payload.get("candidate_inputs")
                if outcome.status is not ReleaseCommandStatusV1.READY:
                    clean_failures.append("clean frozen predecessor did not return READY")
                if type(inputs) is not dict:
                    clean_failures.append("READY payload omitted verified candidate inputs")
                    inputs = {}
                if inputs.get("candidate_commit") != fixture_candidate:
                    clean_failures.append("READY payload candidate identity differs")
                if inputs.get("tree_entry_count") != 515:
                    clean_failures.append("READY payload candidate tree count differs")
                if inputs.get("source_entry_count") != 482:
                    clean_failures.append("READY payload source entry count differs")
                if (
                    inputs.get("source_manifest_sha256")
                    != _DEV0011_SOURCE_MANIFEST_SHA256_V1
                ):
                    clean_failures.append("READY payload source manifest differs")
                if inputs.get("protocol_set_sha256") != _DEV0011_PROTOCOL_SET_SHA256_V1:
                    clean_failures.append("READY payload protocol set differs")
                if inputs.get("tracked_tree_clean") is not True:
                    clean_failures.append("READY payload does not prove tracked cleanliness")
                if output_root.exists():
                    clean_failures.append("build planning created an artifact output path")
                wheel_source = wheel_sources[0]
                wheel_target = fixture / wheel_source.relative_to(source_repository)
                wheel_target.unlink()
                wheel_target.write_bytes(b"DEV-0011 altered wheel fixture\n")
                resource_drift = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                wheel_target.unlink()
                os.link(wheel_source, wheel_target)
                expected_resource_refusal = (
                    ReleaseBuildRefusalCodeV1.RESOURCE_PREFLIGHT_INCOMPLETE.value
                )
                if (
                    resource_drift.status is not ReleaseCommandStatusV1.REFUSED
                    or resource_drift.refusal_code != expected_resource_refusal
                ):
                    clean_failures.append("live wheel drift did not refuse exactly")
                pip_source = source_repository / ".venv/bin/pip"
                pip_target = fixture / ".venv/bin/pip"
                pip_target.unlink()
                pip_target.write_bytes(b"#!/bin/sh\nexit 99\n")
                pip_target.chmod(0o755)
                pip_drift = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                pip_target.unlink()
                os.link(pip_source, pip_target)
                if (
                    pip_drift.status is not ReleaseCommandStatusV1.REFUSED
                    or pip_drift.refusal_code != expected_resource_refusal
                ):
                    clean_failures.append("live packaging-tool drift did not refuse exactly")
                provider_source = (
                    source_repository / ".kirby2/release/clean-providers.toml"
                )
                provider_target = fixture / ".kirby2/release/clean-providers.toml"
                provider_raw = provider_source.read_bytes()
                provider_target.unlink()
                provider_target.write_bytes(
                    provider_raw + b"\n# DEV-0012 raw inventory fingerprint drift\n"
                )
                provider_drift = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                provider_target.unlink()
                os.link(provider_source, provider_target)
                if (
                    provider_drift.status is not ReleaseCommandStatusV1.REFUSED
                    or provider_drift.refusal_code != expected_resource_refusal
                ):
                    clean_failures.append("raw provider-inventory drift did not refuse exactly")
                clean_evidence = {
                    "artifact_output_created": output_root.exists(),
                    "candidate_commit": inputs.get("candidate_commit"),
                    "packaging_tool_drift_refusal_code": pip_drift.refusal_code,
                    "provider_inventory_drift_refusal_code": provider_drift.refusal_code,
                    "resource_drift_refusal_code": resource_drift.refusal_code,
                    "source_entry_count": inputs.get("source_entry_count"),
                    "source_manifest_sha256": inputs.get("source_manifest_sha256"),
                }
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                clean_failures.append(f"clean candidate fixture failed: {type(error).__name__}")

            try:
                reset_fixture(fixture)
                bundle = load_release_protocol_bundle(fixture)
                unresolved = plan_release_build(
                    bundle,
                    candidate_commit="0" * 40,
                    output_root=output_root,
                )
                parent = run_git(
                    fixture,
                    "rev-parse",
                    "--verify",
                    f"{fixture_candidate}^{{commit}}^",
                ).decode("ascii").strip()
                non_head = plan_release_build(
                    bundle,
                    candidate_commit=parent,
                    output_root=output_root,
                )
                run_git(
                    fixture,
                    "replace",
                    fixture_candidate,
                    parent,
                )
                reset_fixture(fixture)
                replacement = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                run_git(
                    fixture,
                    "replace",
                    "-d",
                    fixture_candidate,
                )
                reset_fixture(fixture)
                expected_unresolved = ReleaseBuildRefusalCodeV1.CANDIDATE_COMMIT_INVALID.value
                expected_non_head = ReleaseBuildRefusalCodeV1.CANDIDATE_SOURCE_DIRTY.value
                if (
                    unresolved.status is not ReleaseCommandStatusV1.REFUSED
                    or unresolved.refusal_code != expected_unresolved
                ):
                    identity_failures.append("unresolved commit did not refuse exactly")
                if (
                    non_head.status is not ReleaseCommandStatusV1.REFUSED
                    or non_head.refusal_code != expected_non_head
                ):
                    identity_failures.append("existing non-HEAD commit did not refuse exactly")
                if (
                    replacement.status is not ReleaseCommandStatusV1.REFUSED
                    or replacement.refusal_code != expected_non_head
                ):
                    identity_failures.append("replacement-object checkout did not refuse exactly")
                identity_evidence = {
                    "non_head_refusal_code": non_head.refusal_code,
                    "replacement_refusal_code": replacement.refusal_code,
                    "unresolved_refusal_code": unresolved.refusal_code,
                }
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                identity_failures.append(
                    f"candidate identity fixture failed: {type(error).__name__}"
                )

            try:
                reset_fixture(fixture)
                bundle = load_release_protocol_bundle(fixture)
                source_path = fixture / "kirby2/__init__.py"
                original = source_path.read_bytes()
                source_path.write_bytes(original + b"\n# DEV-0011 unstaged drift\n")
                unstaged = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                reset_fixture(fixture)
                source_path.write_bytes(original + b"\n# DEV-0011 staged drift\n")
                run_git(fixture, "add", "--", "kirby2/__init__.py")
                staged = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                reset_fixture(fixture)
                run_git(
                    fixture,
                    "update-index",
                    "--assume-unchanged",
                    "--",
                    "kirby2/__init__.py",
                )
                source_path.write_bytes(original + b"\n# DEV-0011 hidden tracked drift\n")
                assume_unchanged = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                run_git(
                    fixture,
                    "update-index",
                    "--no-assume-unchanged",
                    "--",
                    "kirby2/__init__.py",
                )
                reset_fixture(fixture)
                run_git(
                    fixture,
                    "update-index",
                    "--skip-worktree",
                    "--",
                    "kirby2/__init__.py",
                )
                source_path.write_bytes(original + b"\n# DEV-0011 skipped tracked drift\n")
                skip_worktree = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                run_git(
                    fixture,
                    "update-index",
                    "--no-skip-worktree",
                    "--",
                    "kirby2/__init__.py",
                )
                reset_fixture(fixture)
                untracked_path = fixture / "kirby2/dev0011_untracked_input.py"
                exclude_path = fixture / ".git/info/exclude"
                exclude_path.write_bytes(
                    exclude_path.read_bytes()
                    + b"\nkirby2/dev0011_untracked_input.py\n"
                )
                untracked_path.write_text("SENTINEL = True\n", encoding="utf-8")
                untracked = plan_release_build(
                    bundle,
                    candidate_commit=fixture_candidate,
                    output_root=output_root,
                )
                untracked_path.unlink()
                expected = ReleaseBuildRefusalCodeV1.CANDIDATE_SOURCE_DIRTY.value
                if (
                    unstaged.status is not ReleaseCommandStatusV1.REFUSED
                    or unstaged.refusal_code != expected
                ):
                    dirty_failures.append("unstaged tracked drift did not refuse exactly")
                if (
                    staged.status is not ReleaseCommandStatusV1.REFUSED
                    or staged.refusal_code != expected
                ):
                    dirty_failures.append("staged tracked drift did not refuse exactly")
                if (
                    assume_unchanged.status is not ReleaseCommandStatusV1.REFUSED
                    or assume_unchanged.refusal_code != expected
                ):
                    dirty_failures.append("assume-unchanged drift did not refuse exactly")
                if (
                    skip_worktree.status is not ReleaseCommandStatusV1.REFUSED
                    or skip_worktree.refusal_code != expected
                ):
                    dirty_failures.append("skip-worktree drift did not refuse exactly")
                if (
                    untracked.status is not ReleaseCommandStatusV1.REFUSED
                    or untracked.refusal_code != expected
                ):
                    dirty_failures.append("untracked build-input drift did not refuse exactly")
                dirty_evidence = {
                    "assume_unchanged_refusal_code": assume_unchanged.refusal_code,
                    "skip_worktree_refusal_code": skip_worktree.refusal_code,
                    "staged_refusal_code": staged.refusal_code,
                    "untracked_refusal_code": untracked.refusal_code,
                    "unstaged_refusal_code": unstaged.refusal_code,
                }
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                dirty_failures.append(f"dirty candidate fixture failed: {type(error).__name__}")
            finally:
                try:
                    reset_fixture(fixture)
                except (OSError, RuntimeError):
                    dirty_failures.append("dirty candidate fixture did not reset")

            try:
                reset_fixture(fixture)
                source_path = fixture / "kirby2/__init__.py"
                source_path.write_bytes(
                    source_path.read_bytes() + b"\n# DEV-0011 committed source drift\n"
                )
                drift_commit = commit_fixture(
                    fixture,
                    "kirby2/__init__.py",
                    "Create source-lock mismatch fixture",
                )
                bundle = load_release_protocol_bundle(fixture)
                outcome = plan_release_build(
                    bundle,
                    candidate_commit=drift_commit,
                    output_root=output_root,
                )
                expected = ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH.value
                if (
                    outcome.status is not ReleaseCommandStatusV1.REFUSED
                    or outcome.refusal_code != expected
                ):
                    source_failures.append(
                        "committed source projection drift did not refuse exactly"
                    )
                source_evidence = {"refusal_code": outcome.refusal_code}
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                source_failures.append(f"source-lock fixture failed: {type(error).__name__}")
            finally:
                try:
                    reset_fixture(fixture)
                except (OSError, RuntimeError):
                    source_failures.append("source-lock fixture did not reset")

            protocol_rows: list[list[str | None]] = []
            for index, relative in enumerate(RELEASE_PROTOCOL_PATHS_V1):
                try:
                    reset_fixture(fixture)
                    stale_bundle = load_release_protocol_bundle(fixture)
                    protocol_path = fixture / relative
                    protocol_path.write_bytes(
                        protocol_path.read_bytes()
                        + f"\n# DEV-0011 protocol drift {index}\n".encode("ascii")
                    )
                    drift_commit = commit_fixture(
                        fixture,
                        relative,
                        f"Create protocol mismatch fixture {index}",
                    )
                    stale_outcome = plan_release_build(
                        stale_bundle,
                        candidate_commit=drift_commit,
                        output_root=output_root,
                    )
                    fresh_outcome = plan_release_build(
                        load_release_protocol_bundle(fixture),
                        candidate_commit=drift_commit,
                        output_root=output_root,
                    )
                    protocol_rows.append(
                        [
                            relative,
                            stale_outcome.refusal_code,
                            fresh_outcome.refusal_code,
                        ]
                    )
                    expected = (
                        ReleaseBuildRefusalCodeV1.CANDIDATE_PROTOCOL_MISMATCH.value
                    )
                    if (
                        stale_outcome.status is not ReleaseCommandStatusV1.REFUSED
                        or stale_outcome.refusal_code != expected
                    ):
                        protocol_failures.append(
                            f"stale-bundle protocol drift did not refuse: {relative}"
                        )
                    if (
                        fresh_outcome.status is not ReleaseCommandStatusV1.REFUSED
                        or fresh_outcome.refusal_code != expected
                    ):
                        protocol_failures.append(
                            f"preflight-bound protocol drift did not refuse: {relative}"
                        )
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    protocol_failures.append(
                        f"protocol fixture failed for {relative}: {type(error).__name__}"
                    )
                finally:
                    try:
                        reset_fixture(fixture)
                    except (OSError, RuntimeError):
                        protocol_failures.append(
                            f"protocol fixture did not reset: {relative}"
                        )
            protocol_evidence = {"protocols": protocol_rows}

    cases = (
        _case(
            "clean_git_object_candidate_is_ready_without_writes",
            "A clean candidate plus exact offline resources is ready; planning creates no artifacts.",
            clean_evidence,
            clean_failures,
        ),
        _case(
            "unresolved_non_head_and_replaced_candidates_are_refused",
            "Missing, non-HEAD, and locally replaced Git objects cannot become candidates.",
            identity_evidence,
            identity_failures,
        ),
        _case(
            "tracked_and_untracked_candidate_drift_is_refused",
            "Working-tree, index flags, and untracked build-input drift fail before dispatch.",
            dirty_evidence,
            dirty_failures,
        ),
        _case(
            "candidate_source_projection_reproduces_the_lock",
            "A committed source change without a regenerated lock is refused.",
            source_evidence,
            source_failures,
        ),
        _case(
            "candidate_protocol_bytes_match_the_loaded_bundle",
            "Each frozen protocol, dependency, and layout path is bound to the loaded protocol set.",
            protocol_evidence,
            protocol_failures,
        ),
    )
    if len(cases) != DEV0011_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0011 release audit inventory changed")
    return ReleaseAuditSuite(
        "DEV-0011",
        cases,
        metadata=(("interrupted_card", "WO40-F"),),
    )


def audit_release_resource_fingerprint_restart() -> ReleaseAuditSuite:
    """Prove every passing live resource fingerprint is report-bound."""

    import kirby2.release.build as release_build_module
    import py_compile

    from kirby2.release.build import (
        ReleaseBuildDistributionV1,
        ReleaseBuildRuntimeSnapshotV1,
        ReleaseResourceItemV1,
        ReleaseResourcePreflightV1,
        load_release_protocol_bundle,
        release_resource_preflight,
    )

    repository = _repository_root()
    bundle = load_release_protocol_bundle(repository)
    provider_inventory = repository / ".kirby2/release/clean-providers.toml"
    live = release_resource_preflight(
        bundle,
        wheelhouse_root=repository / "release/wheelhouse",
        provider_inventory=(provider_inventory if provider_inventory.is_file() else None),
    )
    projection = canonical_json_bytes([item.as_dict() for item in live.items])
    expected_snapshot = _sha256(projection)
    rendered = live.markdown().encode("utf-8")
    snapshot_line = (
        f"Resource snapshot SHA-256: `{expected_snapshot}`\n".encode("ascii")
    )
    snapshot_failures: list[str] = []
    if live.resource_snapshot_sha256 != expected_snapshot:
        snapshot_failures.append("resource snapshot does not match its canonical item projection")
    if rendered.count(snapshot_line) != 1:
        snapshot_failures.append("resource report does not contain exactly one snapshot binding")
    if live.as_dict().get("resource_snapshot_sha256") != expected_snapshot:
        snapshot_failures.append("machine-readable preflight omits the resource snapshot")
    if live.build_runtime is None:
        snapshot_failures.append("passing preflight omits the build runtime snapshot")
    inventory_item = next(
        (item for item in live.items if item.resource_id == "clean-provider-inventory"),
        None,
    )
    observed_inventory_sha256: str | None = None
    if inventory_item is None or not provider_inventory.is_file():
        snapshot_failures.append("live provider inventory resource is unavailable")
    else:
        observed_inventory_sha256 = _sha256(provider_inventory.read_bytes())
        if inventory_item.observed_sha256 != observed_inventory_sha256:
            snapshot_failures.append("raw provider inventory bytes are not fingerprinted")
    snapshot_case = _case(
        "canonical_resource_projection_is_report_bound",
        "The exact ordered resource records, including raw provider bytes, own one report digest.",
        {
            "item_count": len(live.items),
            "provider_inventory_sha256": observed_inventory_sha256,
            "report_sha256": _sha256(rendered),
            "resource_snapshot_sha256": live.resource_snapshot_sha256,
        },
        snapshot_failures,
    )

    runtime_failures: list[str] = []
    altered_runtime_sha256: str | None = None
    altered_environment_sha256: str | None = None
    baseline_runtime_sha256: str | None = None
    external_import_root_refused = False
    inexact_backend_version_refused = False
    nonisolated_startup_refused = False
    user_site_policy_refused = False
    runtime_item = next(
        (item for item in live.items if item.resource_id == "build-runtime-and-backend"),
        None,
    )
    if live.build_runtime is None or runtime_item is None:
        runtime_failures.append("build runtime resource is unavailable")
    else:
        baseline_runtime_sha256 = live.build_runtime.logical_sha256
        if runtime_item.status != "PASS":
            runtime_failures.append("build runtime resource is not passing")
        if runtime_item.observed_sha256 != baseline_runtime_sha256:
            runtime_failures.append("resource item does not bind the build runtime snapshot")
        if tuple(item.name for item in live.build_runtime.distributions) != (
            "pip",
            "setuptools",
        ):
            runtime_failures.append("pip/setuptools distribution order differs")
        if tuple(item.module for item in live.build_runtime.imports) != (
            "pip",
            "setuptools",
            "setuptools.build_meta",
        ):
            runtime_failures.append("resolved build import inventory differs")
        if live.build_runtime.site_packages_file_count <= sum(
            item.file_count for item in live.build_runtime.distributions
        ):
            runtime_failures.append("full site-packages projection is not independently bound")
        if live.build_runtime.python_installation_entry_count <= 0:
            runtime_failures.append("CPython installation projection is empty")
        if (
            live.build_runtime.virtual_environment_entry_count
            <= live.build_runtime.site_packages_file_count
        ):
            runtime_failures.append(
                "full virtual-environment projection is not independently bound"
            )
        configuration_path = repository / ".venv/pyvenv.cfg"
        if (
            not configuration_path.is_file()
            or live.build_runtime.virtual_environment_configuration_sha256
            != _sha256(configuration_path.read_bytes())
        ):
            runtime_failures.append("virtual-environment configuration is not bound")
        if live.build_runtime.effective_import_paths != tuple(
            release_build_module.sys.path
        ):
            runtime_failures.append("effective import roots are not exactly bound")
        nonisolated_probe = subprocess.run(
            [
                os.fspath(repository / ".venv/bin/python"),
                "-c",
                (
                    "from pathlib import Path; "
                    "from kirby2.release.build import inspect_release_build_runtime, "
                    "load_release_protocol_bundle; "
                    "r=Path.cwd().resolve(); "
                    "b=load_release_protocol_bundle(r); "
                    "exec(\"try:\\n inspect_release_build_runtime(b, "
                    "pip_frontend=r/'.venv/bin/pip')\\nexcept ValueError as e:\\n "
                    "print('REFUSED' if 'isolated startup' in str(e) else 'WRONG')"
                    "\\nelse:\\n print('ACCEPTED')\")"
                ),
            ],
            cwd=repository,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            check=False,
        )
        nonisolated_startup_refused = (
            nonisolated_probe.returncode == 0
            and nonisolated_probe.stdout == b"REFUSED\n"
            and not nonisolated_probe.stderr
        )
        if not nonisolated_startup_refused:
            runtime_failures.append("non-isolated Python startup was not refused")
        hostile_environment_digest = (
            "0" * 64
            if live.build_runtime.virtual_environment_configuration_sha256 != "0" * 64
            else "1" * 64
        )
        altered_environment = replace(
            live.build_runtime,
            virtual_environment_configuration_sha256=hostile_environment_digest,
        )
        altered_environment_sha256 = altered_environment.logical_sha256
        if altered_environment_sha256 == baseline_runtime_sha256:
            runtime_failures.append(
                "virtual-environment configuration drift did not change runtime identity"
            )
        original_user_site = release_build_module.site.ENABLE_USER_SITE
        try:
            release_build_module.site.ENABLE_USER_SITE = True
            try:
                release_build_module.inspect_release_build_runtime(
                    bundle,
                    pip_frontend=repository / ".venv/bin/pip",
                )
            except ValueError:
                user_site_policy_refused = True
            else:
                runtime_failures.append("enabled user site-packages were accepted")
        finally:
            release_build_module.site.ENABLE_USER_SITE = original_user_site
        original_import_paths = tuple(release_build_module.sys.path)
        try:
            release_build_module.sys.path.append(
                os.fspath(repository / ".kirby2/unprojected-import-root")
            )
            try:
                release_build_module.inspect_release_build_runtime(
                    bundle,
                    pip_frontend=repository / ".venv/bin/pip",
                )
            except ValueError:
                external_import_root_refused = True
            else:
                runtime_failures.append("unprojected external import root was accepted")
        finally:
            release_build_module.sys.path[:] = original_import_paths
        original_distribution_snapshot = (
            release_build_module._build_distribution_snapshot
        )

        def inexact_distribution_snapshot(
            name: str,
        ) -> tuple[ReleaseBuildDistributionV1, Path]:
            row, root = original_distribution_snapshot(name)
            if name == "setuptools":
                row = replace(row, version="80.9.0rc1")
            return row, root

        try:
            release_build_module._build_distribution_snapshot = (
                inexact_distribution_snapshot
            )
            try:
                release_build_module.inspect_release_build_runtime(
                    bundle,
                    pip_frontend=repository / ".venv/bin/pip",
                )
            except ValueError:
                inexact_backend_version_refused = True
            else:
                runtime_failures.append("inexact setuptools prerelease was accepted")
        finally:
            release_build_module._build_distribution_snapshot = (
                original_distribution_snapshot
            )
        setuptools = live.build_runtime.distributions[1]
        hostile_projection = (
            "0" * 64
            if setuptools.file_projection_sha256 != "0" * 64
            else "1" * 64
        )
        altered_setuptools = ReleaseBuildDistributionV1(
            name=setuptools.name,
            version=setuptools.version,
            file_count=setuptools.file_count,
            file_projection_sha256=hostile_projection,
        )
        altered_runtime = ReleaseBuildRuntimeSnapshotV1(
            runtime=live.build_runtime.runtime,
            python_executable_sha256=live.build_runtime.python_executable_sha256,
            virtual_environment_configuration_sha256=(
                live.build_runtime.virtual_environment_configuration_sha256
            ),
            effective_import_paths=live.build_runtime.effective_import_paths,
            virtual_environment_entry_count=(
                live.build_runtime.virtual_environment_entry_count
            ),
            virtual_environment_projection_sha256=(
                live.build_runtime.virtual_environment_projection_sha256
            ),
            zlib_extension_sha256=live.build_runtime.zlib_extension_sha256,
            zlib_runtime_version=live.build_runtime.zlib_runtime_version,
            archive_encoder_probe_sha256=(
                live.build_runtime.archive_encoder_probe_sha256
            ),
            distributions=(live.build_runtime.distributions[0], altered_setuptools),
            imports=live.build_runtime.imports,
            python_installation_entry_count=(
                live.build_runtime.python_installation_entry_count
            ),
            python_installation_projection_sha256=(
                live.build_runtime.python_installation_projection_sha256
            ),
            site_packages_file_count=live.build_runtime.site_packages_file_count,
            site_packages_projection_sha256=(
                live.build_runtime.site_packages_projection_sha256
            ),
        )
        altered_runtime_sha256 = altered_runtime.logical_sha256
        if altered_runtime_sha256 == baseline_runtime_sha256:
            runtime_failures.append("setuptools file drift did not change runtime identity")
    runtime_case = _case(
        "build_runtime_and_backend_bytes_are_fingerprint_bound",
        "CPython, virtual-environment policy, import roots, zlib, pip, and "
        "setuptools own one identity.",
        {
            "altered_environment_sha256": altered_environment_sha256,
            "altered_runtime_sha256": altered_runtime_sha256,
            "baseline_runtime_sha256": baseline_runtime_sha256,
            "distribution_count": (
                0 if live.build_runtime is None else len(live.build_runtime.distributions)
            ),
            "external_import_root_refused": external_import_root_refused,
            "inexact_backend_version_refused": inexact_backend_version_refused,
            "nonisolated_startup_refused": nonisolated_startup_refused,
            "user_site_policy_refused": user_site_policy_refused,
        },
        runtime_failures,
    )

    shadow_failures: list[str] = []
    shadow_evidence: dict[str, object] = {
        "baseline_file_count": 0,
        "bytecode_projection_changed": False,
        "unrecorded_shadow_refused": False,
    }
    with TemporaryDirectory(prefix="kirby2-release-dev0012-shadow-") as temporary:
        site_packages = Path(temporary) / "site-packages"
        recorded = (
            Path("setuptools/__init__.py"),
            Path("setuptools/build_meta.py"),
            Path("setuptools-80.9.0.dist-info/METADATA"),
        )
        for relative in recorded:
            target = site_packages / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"fixture:{relative.as_posix()}\n".encode("ascii"))

        class _SyntheticDistribution:
            files = recorded
            metadata = {"Name": "setuptools"}
            version = "80.9.0"

            @staticmethod
            def locate_file(relative: object) -> Path:
                return site_packages / Path(str(relative))

        original_distribution = release_build_module.importlib_metadata.distribution
        try:
            release_build_module.importlib_metadata.distribution = (
                lambda _name: _SyntheticDistribution()
            )
            baseline, _base = release_build_module._build_distribution_snapshot(
                "setuptools"
            )
            shadow_evidence["baseline_file_count"] = baseline.file_count
            shadow = site_packages / "setuptools/build_meta/__init__.py"
            shadow.parent.mkdir(parents=True, exist_ok=True)
            shadow.write_text("SENTINEL = True\n", encoding="utf-8")
            try:
                release_build_module._build_distribution_snapshot("setuptools")
            except ValueError:
                shadow_evidence["unrecorded_shadow_refused"] = True
            else:
                shadow_failures.append("unrecorded backend package shadow was accepted")
        except (OSError, TypeError, ValueError) as error:
            shadow_failures.append(
                f"backend shadow fixture failed: {type(error).__name__}"
            )
        finally:
            release_build_module.importlib_metadata.distribution = original_distribution
        bytecode_root = Path(temporary) / "bytecode-site"
        bytecode_source = bytecode_root / "setuptools/build_meta.py"
        bytecode_source.parent.mkdir(parents=True, exist_ok=True)
        bytecode_source.write_text("SENTINEL = 'source'\n", encoding="utf-8")
        try:
            baseline_count, baseline_sha256 = (
                release_build_module._tree_file_projection(
                    bytecode_root,
                    label="synthetic bytecode site-packages",
                )
            )
            py_compile.compile(
                os.fspath(bytecode_source),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
            hostile_count, hostile_sha256 = (
                release_build_module._tree_file_projection(
                    bytecode_root,
                    label="synthetic bytecode site-packages",
                )
            )
            bytecode_changed = (
                hostile_count > baseline_count
                and hostile_sha256 != baseline_sha256
            )
            shadow_evidence["bytecode_projection_changed"] = bytecode_changed
            if not bytecode_changed:
                shadow_failures.append(
                    "unchecked-hash bytecode did not change the environment projection"
                )
        except (OSError, TypeError, ValueError, py_compile.PyCompileError) as error:
            shadow_failures.append(
                f"unchecked-hash bytecode fixture failed: {type(error).__name__}"
            )
    shadow_case = _case(
        "unrecorded_build_backend_shadows_are_refused",
        "Unrecorded backend packages and executable bytecode cannot evade inventory.",
        shadow_evidence,
        shadow_failures,
    )

    drift_failures: list[str] = []
    tool_index = next(
        (
            index
            for index, item in enumerate(live.items)
            if item.resource_id == "project-wheel-frontend"
            and item.status == "PASS"
            and item.observed_sha256 is not None
        ),
        None,
    )
    altered_snapshot: str | None = None
    altered_report_sha256: str | None = None
    if tool_index is None:
        drift_failures.append("passing project wheel frontend fingerprint is unavailable")
    else:
        original = live.items[tool_index]
        hostile_digest = (
            "0" * 64 if original.observed_sha256 != "0" * 64 else "1" * 64
        )
        hostile_item = ReleaseResourceItemV1(
            resource_id=original.resource_id,
            target=original.target,
            kind=original.kind,
            status=original.status,
            expected_sha256=original.expected_sha256,
            observed_sha256=hostile_digest,
            detail=original.detail,
        )
        hostile_items = list(live.items)
        hostile_items[tool_index] = hostile_item
        altered = ReleaseResourcePreflightV1(
            protocol_set_sha256=live.protocol_set_sha256,
            protocol_commit=live.protocol_commit,
            items=tuple(hostile_items),
            build_runtime=live.build_runtime,
            no_network=True,
        )
        altered_snapshot = altered.resource_snapshot_sha256
        altered_report = altered.markdown().encode("utf-8")
        altered_report_sha256 = _sha256(altered_report)
        if live.status != "PASS" or altered.status != "PASS":
            drift_failures.append("fingerprint-only fixture unexpectedly changed readiness")
        if altered_snapshot == live.resource_snapshot_sha256:
            drift_failures.append("changed passing tool bytes did not change the snapshot")
        if altered_report == rendered:
            drift_failures.append("changed passing tool bytes did not change report bytes")
    drift_case = _case(
        "passing_packaging_tool_drift_changes_report_identity",
        "A status-preserving packaging-tool byte change invalidates exact committed evidence.",
        {
            "altered_report_sha256": altered_report_sha256,
            "altered_resource_snapshot_sha256": altered_snapshot,
            "baseline_report_sha256": _sha256(rendered),
            "baseline_resource_snapshot_sha256": live.resource_snapshot_sha256,
        },
        drift_failures,
    )

    cases = (snapshot_case, runtime_case, shadow_case, drift_case)
    if len(cases) != DEV0012_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0012 release audit inventory changed")
    return ReleaseAuditSuite(
        "DEV-0012",
        cases,
        metadata=(("interrupted_card", "WO40-F"),),
    )


def audit_release_artifact_executor_restart() -> ReleaseAuditSuite:
    """Prove the restarted WO40-F executor owns a strict, bounded public seam."""

    import kirby2.release.artifacts as artifacts_module

    from kirby2.release.artifacts import (
        RELEASE_ARTIFACT_BUILD_POLICY_ID_V1,
        RELEASE_BUILD_ATTEMPT_COUNT_V1,
        RELEASE_BUILD_RECORD_SCHEMA_ID_V1,
        ReleaseArtifactBuildRecordV1,
        build_release_artifacts,
        verify_release_artifacts,
    )

    policy_failures: list[str] = []
    if (
        type(RELEASE_ARTIFACT_BUILD_POLICY_ID_V1) is not str
        or RELEASE_ARTIFACT_BUILD_POLICY_ID_V1
        != "KIRBY2_RELEASE_ARTIFACT_BUILD_POLICY_V1"
    ):
        policy_failures.append("artifact-build policy identity differs")
    if (
        type(RELEASE_BUILD_RECORD_SCHEMA_ID_V1) is not str
        or RELEASE_BUILD_RECORD_SCHEMA_ID_V1 != "KIRBY2_RELEASE_BUILD_RECORD_V1"
    ):
        policy_failures.append("build-record schema identity differs")
    if (
        type(RELEASE_BUILD_ATTEMPT_COUNT_V1) is not int
        or RELEASE_BUILD_ATTEMPT_COUNT_V1 != 2
    ):
        policy_failures.append("release build does not require exactly two attempts")
    if not is_dataclass(ReleaseArtifactBuildRecordV1):
        policy_failures.append("build record is not a dataclass")
        record_fields: tuple[str, ...] = ()
    else:
        record_fields = tuple(item.name for item in fields(ReleaseArtifactBuildRecordV1))
        parameters = getattr(ReleaseArtifactBuildRecordV1, "__dataclass_params__", None)
        if parameters is None or not parameters.frozen:
            policy_failures.append("build record is not immutable")
        if not hasattr(ReleaseArtifactBuildRecordV1, "__slots__"):
            policy_failures.append("build record is not slot-bounded")
    if (
        getattr(ReleaseArtifactBuildRecordV1, "schema_id", None)
        != RELEASE_BUILD_RECORD_SCHEMA_ID_V1
    ):
        policy_failures.append("build record does not own the public schema identity")
    for member in ("from_bytes", "canonical_bytes"):
        if not callable(getattr(ReleaseArtifactBuildRecordV1, member, None)):
            policy_failures.append(f"build record omits {member}")
    if not isinstance(getattr(ReleaseArtifactBuildRecordV1, "sha256", None), property):
        policy_failures.append("build record omits its canonical SHA-256 property")
    if "checks" not in record_fields:
        policy_failures.append("build record omits the seven WO40-F check bindings")
    policy_case = _case(
        "artifact_executor_policy_and_record_are_versioned",
        "The two-attempt executor and immutable canonical build record have exact public identities.",
        {
            "attempt_count": RELEASE_BUILD_ATTEMPT_COUNT_V1,
            "policy_id": RELEASE_ARTIFACT_BUILD_POLICY_ID_V1,
            "record_fields": list(record_fields),
            "record_schema_id": RELEASE_BUILD_RECORD_SCHEMA_ID_V1,
        },
        policy_failures,
    )

    parser_failures: list[str] = []
    hostile_records = (
        ("empty_object", canonical_json_bytes({})),
        (
            "wrong_policy",
            canonical_json_bytes(
                {
                    "attempt_count": RELEASE_BUILD_ATTEMPT_COUNT_V1,
                    "policy_id": "KIRBY2_RELEASE_ARTIFACT_BUILD_POLICY_HOSTILE",
                    "schema_id": RELEASE_BUILD_RECORD_SCHEMA_ID_V1,
                }
            ),
        ),
        (
            "wrong_schema",
            canonical_json_bytes(
                {
                    "attempt_count": RELEASE_BUILD_ATTEMPT_COUNT_V1,
                    "policy_id": RELEASE_ARTIFACT_BUILD_POLICY_ID_V1,
                    "schema_id": "KIRBY2_RELEASE_BUILD_RECORD_HOSTILE",
                }
            ),
        ),
        (
            "wrong_attempt_count",
            canonical_json_bytes(
                {
                    "attempt_count": 1,
                    "policy_id": RELEASE_ARTIFACT_BUILD_POLICY_ID_V1,
                    "schema_id": RELEASE_BUILD_RECORD_SCHEMA_ID_V1,
                }
            ),
        ),
        (
            "noncanonical_json",
            (
                b'{ "attempt_count": 2, "policy_id": '
                b'"KIRBY2_RELEASE_ARTIFACT_BUILD_POLICY_V1", "schema_id": '
                b'"KIRBY2_RELEASE_BUILD_RECORD_V1" }\n'
            ),
        ),
    )
    refused: list[str] = []
    unexpected: list[list[str]] = []
    parser = getattr(ReleaseArtifactBuildRecordV1, "from_bytes", None)
    if not callable(parser):
        parser_failures.append("build record has no strict byte parser")
    else:
        for fixture_id, raw in hostile_records:
            try:
                parser(raw)
            except (TypeError, ValueError):
                refused.append(fixture_id)
            except Exception as error:  # pragma: no cover - surfaced as audit evidence
                unexpected.append([fixture_id, type(error).__name__])
                parser_failures.append(
                    f"{fixture_id} produced untyped parser failure {type(error).__name__}"
                )
            else:
                parser_failures.append(f"{fixture_id} was accepted")
    parser_case = _case(
        "build_record_parser_refuses_fixture_drift",
        "Missing, policy-drifted, schema-drifted, one-attempt, and noncanonical records refuse.",
        {
            "fixture_count": len(hostile_records),
            "refused": refused,
            "unexpected": unexpected,
        },
        parser_failures,
    )

    surface_failures: list[str] = []

    def signature_projection(target: object) -> list[list[object]]:
        signature = inspect.signature(target)
        return [
            [
                name,
                parameter.kind.name,
                parameter.default is inspect.Parameter.empty,
            ]
            for name, parameter in signature.parameters.items()
        ]

    expected_build_signature = [
        ["bundle", "POSITIONAL_OR_KEYWORD", True],
        ["candidate_commit", "KEYWORD_ONLY", True],
        ["artifact_root", "KEYWORD_ONLY", True],
    ]
    expected_verify_signature = [
        ["bundle", "POSITIONAL_OR_KEYWORD", True],
        ["artifact_root", "KEYWORD_ONLY", True],
        ["candidate_commit", "KEYWORD_ONLY", False],
    ]
    observed_build_signature: list[list[object]] = []
    observed_verify_signature: list[list[object]] = []
    if not callable(build_release_artifacts):
        surface_failures.append("public build executor is not callable")
    else:
        observed_build_signature = signature_projection(build_release_artifacts)
        if observed_build_signature != expected_build_signature:
            surface_failures.append("public build executor signature differs")
    if not callable(verify_release_artifacts):
        surface_failures.append("public artifact verifier is not callable")
    else:
        observed_verify_signature = signature_projection(verify_release_artifacts)
        if observed_verify_signature != expected_verify_signature:
            surface_failures.append("public artifact verifier signature differs")
    for label, target in (
        ("build", build_release_artifacts),
        ("verify", verify_release_artifacts),
    ):
        if getattr(target, "__module__", None) != "kirby2.release.artifacts":
            surface_failures.append(f"public {label} surface is not owned by artifacts")
    build_network_gate_calls = 0
    verify_network_gate_calls = 0
    try:
        artifact_tree = ast.parse(inspect.getsource(artifacts_module))

        def artifact_function(name: str) -> ast.FunctionDef | None:
            return next(
                (
                    item
                    for item in ast.walk(artifact_tree)
                    if isinstance(item, ast.FunctionDef) and item.name == name
                ),
                None,
            )

        def network_gate_calls(node: ast.FunctionDef | None) -> int:
            if node is None:
                return 0
            return sum(
                1
                for item in ast.walk(node)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "_require_codex_network_seatbelt"
            )

        build_network_gate_calls = network_gate_calls(
            artifact_function("build_release_artifacts")
        )
        verify_network_gate_calls = network_gate_calls(
            artifact_function("verify_release_artifacts")
        )
        if build_network_gate_calls != 1:
            surface_failures.append(
                "artifact construction does not own exactly one Codex network gate"
            )
        if verify_network_gate_calls != 0:
            surface_failures.append(
                "provider-free artifact verification retained a build-only network gate"
            )
    except (OSError, TypeError, SyntaxError) as error:
        surface_failures.append(
            f"artifact network-boundary AST failed: {type(error).__name__}"
        )
    surface_case = _case(
        "artifact_build_and_verification_surfaces_are_bounded",
        "The bounded builder retains network denial while read-only verification remains provider-free.",
        {
            "build_signature": observed_build_signature,
            "build_network_gate_calls": build_network_gate_calls,
            "executor_invocations": 0,
            "verify_signature": observed_verify_signature,
            "verify_network_gate_calls": verify_network_gate_calls,
        },
        surface_failures,
    )

    cases = (policy_case, parser_case, surface_case)
    if len(cases) != DEV0013_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0013 release audit inventory changed")
    return ReleaseAuditSuite(
        "DEV-0013",
        cases,
        metadata=(("interrupted_card", "WO40-F"),),
    )


def audit_release_qualification_executor_restart() -> ReleaseAuditSuite:
    """Prove the restarted WO40-G executor is strict without running a provider."""

    import kirby2.release.qualification as qualification_module
    import kirby2.release.qualification_executor as executor_module
    import kirby2.release.qualification_worker as worker_module
    from kirby2.release.qualification import (
        RELEASE_FUNCTIONAL_STEP_ORDER_V1,
        RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1,
        RELEASE_QUALIFICATION_ATTEMPT_SCHEMA_ID_V1,
        load_release_qualification_protocol,
        verify_release_qualification,
    )
    from kirby2.release.qualification_executor import (
        TART_BASE_CONFIG_SHA256_V1,
        TART_BASE_DISK_BYTES_V1,
        TART_BASE_DISK_MODE_V1,
        TART_BASE_DISK_SHA256_V1,
        TART_BASE_NVRAM_SHA256_V1,
        TART_BASE_VM_V1,
        TART_EXECUTABLE_MODE_V1,
        TART_EXECUTABLE_SHA256_V1,
        TART_EXECUTABLE_V1,
        TART_PROVIDER_POLICY_ID_V1,
        TART_TARGET_ID_V1,
        TART_VERSION_V1,
        execute_release_qualification,
    )
    from kirby2.release.qualification_records import (
        RELEASE_CLEAN_PROVIDER_ATTESTATION_SCHEMA_ID_V1,
        RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1,
        RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1,
        RELEASE_QUALIFICATION_CHECK_COUNT_V1,
        RELEASE_QUALIFICATION_CHECK_PROOF_SCHEMA_ID_V1,
        RELEASE_QUALIFICATION_EXECUTION_POLICY_ID_V1,
        RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1,
        RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1,
        RELEASE_QUALIFICATION_ROOT_ROLE_ORDER_V1,
        RELEASE_QUALIFICATION_STEP_COUNT_V1,
        ReleaseCleanProviderAttestationV1,
        ReleaseQualificationArtifactBindingV1,
        ReleaseQualificationAttemptV1,
        ReleaseQualificationCommandObservationV1,
        ReleaseQualificationFactsV1,
        ReleaseQualificationNetworkScopeV1,
        ReleaseQualificationRootObservationV1,
        ReleaseQualificationSessionV1,
        ReleaseQualificationStepObservationV1,
        ReleaseQualificationStepResultV1,
        build_release_qualification_attempt_record,
        required_release_qualification_check_ids,
        verify_release_qualification_record,
    )

    record_failures: list[str] = []
    record_types = (
        ReleaseCleanProviderAttestationV1,
        ReleaseQualificationAttemptV1,
        ReleaseQualificationStepResultV1,
    )
    for record_type in record_types:
        if not is_dataclass(record_type):
            record_failures.append(f"{record_type.__name__} is not a dataclass")
            continue
        parameters = getattr(record_type, "__dataclass_params__", None)
        if parameters is None or not parameters.frozen:
            record_failures.append(f"{record_type.__name__} is not immutable")
        if not hasattr(record_type, "__slots__"):
            record_failures.append(f"{record_type.__name__} is not slot-bounded")
    exact_identities = {
        "attempt_schema": (
            RELEASE_QUALIFICATION_ATTEMPT_SCHEMA_ID_V1,
            "KIRBY2_RELEASE_QUALIFICATION_ATTEMPT_V1",
        ),
        "check_proof_schema": (
            RELEASE_QUALIFICATION_CHECK_PROOF_SCHEMA_ID_V1,
            "KIRBY2_RELEASE_QUALIFICATION_CHECK_PROOF_V1",
        ),
        "execution_policy": (
            RELEASE_QUALIFICATION_EXECUTION_POLICY_ID_V1,
            "KIRBY2_RELEASE_QUALIFICATION_EXECUTION_POLICY_V1",
        ),
        "provider_schema": (
            RELEASE_CLEAN_PROVIDER_ATTESTATION_SCHEMA_ID_V1,
            "KIRBY2_RELEASE_CLEAN_PROVIDER_ATTESTATION_V1",
        ),
    }
    for label, (observed, expected) in exact_identities.items():
        if type(observed) is not str or observed != expected:
            record_failures.append(f"qualification {label} identity differs")
    if (
        type(RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1) is not int
        or RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1 != 1
    ):
        record_failures.append("qualification record schema version differs")
    if (
        type(RELEASE_QUALIFICATION_STEP_COUNT_V1) is not int
        or RELEASE_QUALIFICATION_STEP_COUNT_V1 != 38
        or RELEASE_QUALIFICATION_STEP_COUNT_V1
        != 2 * len(RELEASE_FUNCTIONAL_STEP_ORDER_V1)
        + len(RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1)
    ):
        record_failures.append("qualification matrix does not contain exactly 38 rows")
    check_ids = required_release_qualification_check_ids("macos-arm64")
    if (
        type(RELEASE_QUALIFICATION_CHECK_COUNT_V1) is not int
        or RELEASE_QUALIFICATION_CHECK_COUNT_V1 != 42
        or len(check_ids) != RELEASE_QUALIFICATION_CHECK_COUNT_V1
        or len(check_ids) != len(set(check_ids))
    ):
        record_failures.append("qualification proof does not contain 42 unique checks")
    provider_fields = tuple(
        item.name for item in fields(ReleaseCleanProviderAttestationV1)
    )
    attempt_fields = tuple(item.name for item in fields(ReleaseQualificationAttemptV1))
    result_fields = tuple(item.name for item in fields(ReleaseQualificationStepResultV1))
    expected_provider_fields = (
        "provider_id",
        "target_id",
        "provider_inventory_sha256",
        "provider_capability_sha256",
        "provider_adapter_id",
        "attestation_method",
        "system",
        "os_version",
        "kernel_release",
        "machine",
        "machine_model",
        "python_implementation",
        "python_version",
        "cpu_count",
        "memory_bytes",
        "available_disk_bytes",
        "offline_install",
        "network_scope",
        "observed_at_utc",
    )
    expected_attempt_fields = (
        "gate_id",
        "target_id",
        "candidate_commit",
        "protocol_set_sha256",
        "source_manifest_sha256",
        "artifact_index_sha256",
        "build_evidence_sha256",
        "session",
        "commands",
        "steps",
        "facts",
        "checks",
        "warnings",
        "status",
    )
    if provider_fields != expected_provider_fields:
        record_failures.append("provider-attestation public fields differ")
    if attempt_fields != expected_attempt_fields:
        record_failures.append("qualification-attempt public fields differ")
    if result_fields != ("result_id", "size", "sha256", "payload"):
        record_failures.append("step result does not embed its canonical payload")
    if not callable(getattr(ReleaseQualificationStepResultV1, "from_payload", None)):
        record_failures.append("step result omits its payload-derived constructor")
    for record_type in (
        ReleaseCleanProviderAttestationV1,
        ReleaseQualificationAttemptV1,
    ):
        for member in ("from_bytes", "canonical_bytes"):
            if not callable(getattr(record_type, member, None)):
                record_failures.append(f"{record_type.__name__} omits {member}")
        if not isinstance(getattr(record_type, "sha256", None), property):
            record_failures.append(f"{record_type.__name__} omits canonical SHA-256")
    records_case = _case(
        "qualification_records_and_reconstruction_are_versioned",
        "Immutable provider/attempt records embed step payloads and own 38 rows plus 42 checks.",
        {
            "attempt_fields": list(attempt_fields),
            "check_count": RELEASE_QUALIFICATION_CHECK_COUNT_V1,
            "executor_invocations": 0,
            "provider_fields": list(provider_fields),
            "record_schema_version": RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1,
            "step_count": RELEASE_QUALIFICATION_STEP_COUNT_V1,
            "step_result_fields": list(result_fields),
        },
        record_failures,
    )

    parser_failures: list[str] = []
    refused: list[str] = []
    unexpected: list[list[str]] = []
    valid_attempt_sha256: str | None = None
    verification_projection: dict[str, object] | None = None

    def synthetic_records() -> tuple[
        ReleaseCleanProviderAttestationV1,
        ReleaseQualificationAttemptV1,
    ]:
        digest = _sha256(b"DEV-0014 synthetic qualification fixture")
        empty_inventory = _sha256(canonical_json_bytes([]))
        provider = ReleaseCleanProviderAttestationV1(
            provider_id=f"kirby2-clean-provider-macos-arm64-{digest[:16]}",
            target_id="macos-arm64",
            provider_inventory_sha256=digest,
            provider_capability_sha256=digest,
            provider_adapter_id="TART_LOCAL_VM_V1",
            attestation_method=RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1,
            system="Darwin",
            os_version="synthetic-1",
            kernel_release="synthetic-1",
            machine="arm64",
            machine_model="SyntheticMac",
            python_implementation="CPython",
            python_version="3.14.0",
            cpu_count=4,
            memory_bytes=8 * 1024**3,
            available_disk_bytes=20 * 1024**3,
            offline_install=True,
            network_scope=ReleaseQualificationNetworkScopeV1.HOST_ONLY,
            observed_at_utc="2026-08-31T00:00:00Z",
        )
        bindings = tuple(
            ReleaseQualificationArtifactBindingV1(
                artifact_id=artifact_id,
                size=1,
                release_store_sha256=digest,
                provider_copy_sha256=digest,
            )
            for artifact_id in RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1[
                "macos-arm64"
            ]
        )
        roots = tuple(
            ReleaseQualificationRootObservationV1(
                root_role=root_role,
                root_id=f"fixture-{index}",
                data_root=f"/tmp/kirby2-dev0014-fixture-{index}",
                initial_state="ABSENT",
                initial_inventory_sha256=empty_inventory,
            )
            for index, root_role in enumerate(
                RELEASE_QUALIFICATION_ROOT_ROLE_ORDER_V1,
                start=1,
            )
        )
        session = ReleaseQualificationSessionV1(
            session_id="wo40g-synthetic-fixture",
            provider_id=provider.provider_id,
            provider_attestation_sha256=provider.sha256,
            provider_instance_id="tart-instance-synthetic",
            target_id="macos-arm64",
            attempt_number=1,
            started_at_utc="2026-08-31T00:00:00Z",
            finished_at_utc="2026-08-31T00:00:01Z",
            duration_ns=1_000_000_000,
            network_scope=ReleaseQualificationNetworkScopeV1.HOST_ONLY,
            installation_source=RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1,
            source_checkout_present=False,
            artifact_bindings=bindings,
            roots=roots,
        )
        functional_roles = (
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "SECONDARY_CLEAN_ROOT",
            "SECONDARY_CLEAN_ROOT",
            "BOTH_CLEAN_ROOTS",
            "RESTORE_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
            "PRIMARY_CLEAN_ROOT",
        )
        functional = tuple(zip(RELEASE_FUNCTIONAL_STEP_ORDER_V1, functional_roles))
        rows = (
            *(("macos-arm64/desktop", step_id, root_role) for step_id, root_role in functional),
            *(("macos-arm64/headless", step_id, root_role) for step_id, root_role in functional),
            *(
                ("macos-arm64/headless", step_id, "PRIMARY_CLEAN_ROOT")
                for step_id in RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1
            ),
        )
        command = ReleaseQualificationCommandObservationV1(
            command_id="fixture-command-1",
            sequence=1,
            artifact_selector="macos-arm64/desktop",
            root_role="PRIMARY_CLEAN_ROOT",
            argv=("kirby2-fixture", "CLEAN_INSTALL"),
            environment_sha256=digest,
            started_at_utc="2026-08-31T00:00:00Z",
            duration_ns=1,
            returncode=0,
            timed_out=False,
            stdout_size=0,
            stdout_sha256=_sha256(b""),
            stderr_size=0,
            stderr_sha256=_sha256(b""),
        )
        embedded = ReleaseQualificationStepResultV1.from_payload(
            "macos-arm64/desktop:CLEAN_INSTALL",
            {
                "distributions": [
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
                ],
                "execution_index": 1,
                "offline_install": True,
                "source_checkout_present": False,
            },
        )
        steps = tuple(
            ReleaseQualificationStepObservationV1(
                artifact_selector=selector,
                step_id=step_id,
                root_role=root_role,
                command_ids=("fixture-command-1",) if position == 0 else (),
                duration_ns=1 if position == 0 else 0,
                status="PASS" if position == 0 else "NOT_EXERCISED",
                warning_codes=(),
                result=embedded if position == 0 else None,
            )
            for position, (selector, step_id, root_role) in enumerate(rows)
        )
        facts = ReleaseQualificationFactsV1(
            clean_environment=False,
            cross_platform_integer_core_sha256=digest,
            desktop_run_sha256=digest,
            headless_run_sha256=digest,
            platform_id="macos-arm64",
            replay_sha256=digest,
        )
        attempt = build_release_qualification_attempt_record(
            gate_id="WO40-G",
            target_id="macos-arm64",
            candidate_commit="1" * 40,
            protocol_set_sha256=digest,
            source_manifest_sha256=digest,
            artifact_index_sha256=digest,
            build_evidence_sha256=digest,
            session=session,
            commands=(command,),
            steps=steps,
            facts=facts,
        )
        return provider, attempt

    try:
        fixture_provider, fixture_attempt = synthetic_records()
        fixture_provider_raw = fixture_provider.canonical_bytes()
        fixture_attempt_raw = fixture_attempt.canonical_bytes()
        restored_provider = ReleaseCleanProviderAttestationV1.from_bytes(
            fixture_provider_raw
        )
        restored_attempt = ReleaseQualificationAttemptV1.from_bytes(fixture_attempt_raw)
        protocol = load_release_qualification_protocol(
            (_repository_root() / "release/qualification.toml").resolve(strict=True)
        )
        verified = verify_release_qualification_record(
            restored_provider,
            restored_attempt,
            protocol,
        )
        verification_projection = verified.as_dict()
        valid_attempt_sha256 = restored_attempt.sha256
        if (
            restored_provider.canonical_bytes() != fixture_provider_raw
            or restored_attempt.canonical_bytes() != fixture_attempt_raw
            or len(restored_attempt.steps) != 38
            or len(restored_attempt.checks) != 42
            or verified.check_count != 42
        ):
            parser_failures.append("valid canonical qualification fixture did not round-trip")

        def fresh_attempt() -> dict[str, object]:
            value = load_canonical_json_bytes(
                fixture_attempt_raw,
                "DEV-0014 synthetic qualification attempt",
            )
            if type(value) is not dict:
                raise TypeError("synthetic qualification attempt is not an object")
            return value

        hostile_records: list[tuple[str, object, bytes]] = []
        hostile_records.append(
            (
                "attempt_noncanonical",
                ReleaseQualificationAttemptV1.from_bytes,
                b" " + fixture_attempt_raw,
            )
        )
        wrong_schema = fresh_attempt()
        wrong_schema["schema_id"] = "KIRBY2_RELEASE_QUALIFICATION_ATTEMPT_HOSTILE"
        hostile_records.append(
            (
                "attempt_wrong_schema",
                ReleaseQualificationAttemptV1.from_bytes,
                canonical_json_bytes(wrong_schema),
            )
        )
        wrong_order = fresh_attempt()
        ordered_steps = wrong_order["steps"]
        if type(ordered_steps) is not list:
            raise TypeError("synthetic step inventory is not an array")
        ordered_steps[0], ordered_steps[1] = ordered_steps[1], ordered_steps[0]
        hostile_records.append(
            (
                "attempt_wrong_order",
                ReleaseQualificationAttemptV1.from_bytes,
                canonical_json_bytes(wrong_order),
            )
        )
        wrong_count = fresh_attempt()
        counted_steps = wrong_count["steps"]
        if type(counted_steps) is not list:
            raise TypeError("synthetic step inventory is not an array")
        counted_steps.pop()
        hostile_records.append(
            (
                "attempt_wrong_count",
                ReleaseQualificationAttemptV1.from_bytes,
                canonical_json_bytes(wrong_count),
            )
        )
        wrong_target = fresh_attempt()
        wrong_target["target_id"] = "linux-x86_64"
        hostile_records.append(
            (
                "attempt_wrong_target",
                ReleaseQualificationAttemptV1.from_bytes,
                canonical_json_bytes(wrong_target),
            )
        )
        forged_checks = fresh_attempt()
        checks = forged_checks["checks"]
        if type(checks) is not list or not checks or type(checks[0]) is not dict:
            raise TypeError("synthetic check inventory is malformed")
        observed_check_digest = checks[0]["evidence_sha256"]
        checks[0]["evidence_sha256"] = (
            "f" * 64 if observed_check_digest != "f" * 64 else "e" * 64
        )
        hostile_records.append(
            (
                "attempt_forged_checks",
                ReleaseQualificationAttemptV1.from_bytes,
                canonical_json_bytes(forged_checks),
            )
        )
        forged_result = fresh_attempt()
        result_steps = forged_result["steps"]
        if type(result_steps) is not list or not result_steps or type(result_steps[0]) is not dict:
            raise TypeError("synthetic result step is malformed")
        result = result_steps[0]["result"]
        if type(result) is not dict or type(result.get("payload")) is not dict:
            raise TypeError("synthetic embedded result is malformed")
        result["payload"]["offline_install"] = False
        forged_payload_raw = canonical_json_bytes(result["payload"])
        result["size"] = len(forged_payload_raw)
        result["sha256"] = _sha256(forged_payload_raw)
        hostile_records.append(
            (
                "attempt_forged_embedded_result",
                ReleaseQualificationAttemptV1.from_bytes,
                canonical_json_bytes(forged_result),
            )
        )
        provider_wrong_schema = load_canonical_json_bytes(
            fixture_provider_raw,
            "DEV-0014 synthetic provider attestation",
        )
        if type(provider_wrong_schema) is not dict:
            raise TypeError("synthetic provider attestation is not an object")
        provider_wrong_schema["schema_id"] = (
            "KIRBY2_RELEASE_CLEAN_PROVIDER_ATTESTATION_HOSTILE"
        )
        hostile_records.append(
            (
                "provider_wrong_schema",
                ReleaseCleanProviderAttestationV1.from_bytes,
                canonical_json_bytes(provider_wrong_schema),
            )
        )
        for fixture_id, parser, raw in hostile_records:
            try:
                parser(raw)  # type: ignore[operator]
            except (TypeError, ValueError):
                refused.append(fixture_id)
            except Exception as error:  # pragma: no cover - surfaced as audit evidence
                unexpected.append([fixture_id, type(error).__name__])
                parser_failures.append(
                    f"{fixture_id} produced untyped parser failure {type(error).__name__}"
                )
            else:
                parser_failures.append(f"{fixture_id} was accepted")
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        parser_failures.append(
            f"canonical qualification fixture failed: {type(error).__name__}"
        )
    parser_case = _case(
        "qualification_parsers_reconstruct_and_refuse_forgery",
        "Canonical records round-trip while order, count, target, schema, result, and checks fail closed.",
        {
            "executor_invocations": 0,
            "fixture_count": len(refused) + len(unexpected),
            "refused": refused,
            "unexpected": unexpected,
            "valid_attempt_sha256": valid_attempt_sha256,
            "verification": verification_projection,
        },
        parser_failures,
    )

    surface_failures: list[str] = []
    digest_refusals: list[str] = []

    def signature_projection(target: object) -> list[list[object]]:
        signature = inspect.signature(target)
        return [
            [
                name,
                parameter.kind.name,
                parameter.default is inspect.Parameter.empty,
            ]
            for name, parameter in signature.parameters.items()
        ]

    expected_executor_signature = [
        ["bundle", "POSITIONAL_OR_KEYWORD", True],
        ["target_id", "KEYWORD_ONLY", True],
        ["build_evidence", "KEYWORD_ONLY", True],
        ["artifact_root", "KEYWORD_ONLY", True],
    ]
    expected_verifier_signature = [
        ["bundle", "POSITIONAL_OR_KEYWORD", True],
        ["target_id", "KEYWORD_ONLY", True],
        ["build_evidence", "KEYWORD_ONLY", True],
        ["artifact_root", "KEYWORD_ONLY", True],
    ]
    observed_executor_signature = signature_projection(execute_release_qualification)
    observed_verifier_signature = signature_projection(verify_release_qualification)
    observed_worker_signature = signature_projection(worker_module.main)
    if observed_executor_signature != expected_executor_signature:
        surface_failures.append("public qualification executor signature differs")
    if observed_verifier_signature != expected_verifier_signature:
        surface_failures.append("public qualification verifier signature differs")
    if observed_worker_signature != [["argv", "POSITIONAL_OR_KEYWORD", False]]:
        surface_failures.append("installed qualification worker main signature differs")
    if (
        getattr(execute_release_qualification, "__module__", None)
        != "kirby2.release.qualification_executor"
        or "execute_release_qualification"
        not in getattr(executor_module, "__all__", ())
    ):
        surface_failures.append("qualification executor is not owned by its closed module")
    if (
        getattr(verify_release_qualification, "__module__", None)
        != "kirby2.release.qualification"
        or "verify_release_qualification"
        not in getattr(qualification_module, "__all__", ())
    ):
        surface_failures.append("qualification verifier is not public from qualification")
    tart_constants = {
        "base_config_sha256": TART_BASE_CONFIG_SHA256_V1,
        "base_disk_bytes": TART_BASE_DISK_BYTES_V1,
        "base_disk_mode": TART_BASE_DISK_MODE_V1,
        "base_disk_sha256": TART_BASE_DISK_SHA256_V1,
        "base_nvram_sha256": TART_BASE_NVRAM_SHA256_V1,
        "base_vm": TART_BASE_VM_V1,
        "executable": os.fspath(TART_EXECUTABLE_V1),
        "executable_mode": TART_EXECUTABLE_MODE_V1,
        "executable_sha256": TART_EXECUTABLE_SHA256_V1,
        "provider_policy": TART_PROVIDER_POLICY_ID_V1,
        "target": TART_TARGET_ID_V1,
        "version": TART_VERSION_V1,
    }
    expected_tart_constants = {
        "base_config_sha256": (
            "1f1175a3731ab4fbb1beff6990775fdbfb00931a10c7d43be87915a766699d8c"
        ),
        "base_disk_bytes": 80_000_000_000,
        "base_disk_mode": 0o644,
        "base_disk_sha256": (
            "fa88c6aae58badcf38943ee2c85cf9070d0a79ceff801c2114a9767f82f572a3"
        ),
        "base_nvram_sha256": (
            "69078eae7ddf3193411d98f27931674a6abfd75fca229f082591525eddea8387"
        ),
        "base_vm": "kirby2-dev0014-macos-offline-base-hardened",
        "executable": "/opt/homebrew/Cellar/tart/2.32.1/bin/tart",
        "executable_mode": 0o555,
        "executable_sha256": (
            "44137d8dba251d4a4f9a113ecc8619d821ea7ea0f28217a88f57dde894d83d76"
        ),
        "provider_policy": "KIRBY2_TART_HOST_ONLY_HARDENED_PROVIDER_V1",
        "target": "macos-arm64",
        "version": "2.32.1",
    }
    if tart_constants != expected_tart_constants:
        surface_failures.append("fixed Tart provider constants differ")

    try:
        concrete_path = Path(executor_module.__file__).resolve(strict=True)
        if (
            executor_module._absolute_input(concrete_path, "executor source")
            != concrete_path
        ):
            surface_failures.append("qualification path normalization differs")
        path_snapshot = executor_module._stable_read(
            concrete_path,
            maximum_bytes=4 * 1024 * 1024,
            require_read_only=False,
        )
        if not path_snapshot.raw:
            surface_failures.append("qualification concrete Path probe is empty")
        path_digest = executor_module._stable_file_digest(
            concrete_path,
            expected_bytes=len(path_snapshot.raw),
        )
        if (
            path_digest.sha256 != path_snapshot.sha256
            or path_digest.identity != path_snapshot.identity
        ):
            surface_failures.append("streamed provider digest differs from stable read")
    except (OSError, TypeError, ValueError) as error:
        surface_failures.append(
            f"qualification concrete Path probe failed: {type(error).__name__}"
        )
    try:
        with TemporaryDirectory(prefix="kirby2-release-dev0014-digest-") as temporary:
            fixture = Path(temporary) / "provider.img"
            fixture.write_bytes(b"fixed-provider-digest-fixture")
            fixture.chmod(0o644)
            fixture_bytes = fixture.stat().st_size

            def require_digest_refusal(fixture_id: str, target: Path, size: int) -> None:
                try:
                    executor_module._stable_file_digest(target, expected_bytes=size)
                except (OSError, ValueError):
                    digest_refusals.append(fixture_id)
                else:
                    surface_failures.append(
                        f"provider digest accepted hostile fixture: {fixture_id}"
                    )

            symlink = Path(temporary) / "provider-symlink.img"
            symlink.symlink_to(fixture)
            require_digest_refusal("symlink", symlink, fixture_bytes)

            hardlink = Path(temporary) / "provider-hardlink.img"
            os.link(fixture, hardlink)
            require_digest_refusal("hardlink", fixture, fixture_bytes)
            hardlink.unlink()

            require_digest_refusal("wrong-size", fixture, fixture_bytes + 1)
            fixture.chmod(0o666)
            require_digest_refusal("writable-mode", fixture, fixture_bytes)
            fixture.chmod(0o644)

            original_read = executor_module.os.read
            try:
                executor_module.os.read = lambda _descriptor, _size: b""
                require_digest_refusal("short-read", fixture, fixture_bytes)
            finally:
                executor_module.os.read = original_read

            original_identity = executor_module._identity
            identity_calls = 0

            def drifting_identity(metadata: os.stat_result) -> tuple[int, ...]:
                nonlocal identity_calls
                identity_calls += 1
                value = original_identity(metadata)
                if identity_calls == 2:
                    return (*value[:-1], value[-1] + 1)
                return value

            try:
                executor_module._identity = drifting_identity
                require_digest_refusal("changed-identity", fixture, fixture_bytes)
            finally:
                executor_module._identity = original_identity
    except (OSError, TypeError, ValueError) as error:
        surface_failures.append(
            f"provider digest hostile fixtures failed: {type(error).__name__}"
        )
    if sorted(digest_refusals) != [
        "changed-identity",
        "hardlink",
        "short-read",
        "symlink",
        "writable-mode",
        "wrong-size",
    ]:
        surface_failures.append("provider digest hostile-refusal inventory differs")
    try:
        diagnostic = executor_module._diagnostic_excerpt(
            b"root privilege required\nRuntimeFailed\tSoftnet\r",
            "DEV-0014 diagnostic fixture",
        )
        if diagnostic != "root privilege required RuntimeFailed Softnet":
            surface_failures.append("qualification diagnostic canonicalization differs")
    except (TypeError, ValueError) as error:
        surface_failures.append(
            f"qualification diagnostic canonicalization failed: {type(error).__name__}"
        )

    try:
        executor_tree = ast.parse(inspect.getsource(executor_module))
        worker_tree = ast.parse(inspect.getsource(worker_module))
    except (OSError, TypeError, SyntaxError) as error:
        surface_failures.append(f"qualification module AST failed: {type(error).__name__}")
        executor_tree = ast.Module(body=[], type_ignores=[])
        worker_tree = ast.Module(body=[], type_ignores=[])

    def function_node(tree: ast.AST, name: str) -> ast.FunctionDef | None:
        return next(
            (
                item
                for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == name
            ),
            None,
        )

    provider_node = function_node(executor_tree, "_require_tart_provider")
    guest_provider_node = function_node(executor_tree, "_prove_guest_provider")
    digest_node = function_node(executor_tree, "_stable_file_digest")
    clone_node = function_node(executor_tree, "_create_clone")
    executor_node = function_node(
        executor_tree, "_execute_macos_release_qualification"
    )
    digest_names = (
        set()
        if digest_node is None
        else {
            item.id
            for item in ast.walk(digest_node)
            if isinstance(item, ast.Name)
        }
    )
    digest_literals = (
        set()
        if digest_node is None
        else {
            item.value
            for item in ast.walk(digest_node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    )
    digest_fstat_calls = (
        0
        if digest_node is None
        else sum(
            1
            for item in ast.walk(digest_node)
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "fstat"
        )
    )
    digest_attributes = (
        set()
        if digest_node is None
        else {
            item.attr
            for item in ast.walk(digest_node)
            if isinstance(item, ast.Attribute)
        }
    )
    if (
        digest_node is None
        or "_PROVIDER_DIGEST_CHUNK_BYTES" not in digest_names
        or "_identity" not in digest_names
        or "O_NOFOLLOW" not in digest_literals
        or digest_fstat_calls != 2
        or not {
            "getuid",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_uid",
        }.issubset(digest_attributes)
    ):
        surface_failures.append("streamed provider digest structure is not fail-closed")

    provider_names = (
        set()
        if provider_node is None
        else {
            item.id
            for item in ast.walk(provider_node)
            if isinstance(item, ast.Name)
        }
    )
    if "TART_EXECUTABLE_MODE_V1" not in provider_names:
        surface_failures.append("Tart provider omits its executable-mode binding")
    if "TART_BASE_DISK_SHA256_V1" not in provider_names:
        surface_failures.append("Tart provider omits its whole-disk content binding")
    provider_source = "" if provider_node is None else ast.unparse(provider_node)
    clone_source = "" if clone_node is None else ast.unparse(clone_node)
    required_provider_bindings = {
        "disk.sha256 != TART_BASE_DISK_SHA256_V1",
        "_identity(disk_metadata) != disk.identity",
        "stat.S_IMODE(disk_metadata.st_mode) != TART_BASE_DISK_MODE_V1",
        "QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH",
    }
    if not all(binding in provider_source for binding in required_provider_bindings):
        surface_failures.append("fixed provider omits a disk identity or refusal binding")
    if provider_source.count("except (OSError, ValueError) as error:") < 2:
        surface_failures.append("fixed provider does not normalize file identity refusals")
    required_clone_bindings = {
        "disk.sha256 != TART_BASE_DISK_SHA256_V1",
        "nvram.sha256 != TART_BASE_NVRAM_SHA256_V1",
        "projection != base_projection",
        "stat.S_IMODE(disk_metadata.st_mode) != TART_BASE_DISK_MODE_V1",
        "QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH",
    }
    if not all(binding in clone_source for binding in required_clone_bindings):
        surface_failures.append("qualification clone omits a preboot provider binding")

    sequential_clone_loops = 0
    if executor_node is not None:
        for item in ast.walk(executor_node):
            if not isinstance(item, ast.For):
                continue
            called = {
                node.func.id
                for node in ast.walk(item)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            if {"_create_clone", "_run_form"}.issubset(called):
                sequential_clone_loops += 1
    if sequential_clone_loops != 1:
        surface_failures.append(
            "clone verification and workload are not one sequential lifecycle"
        )

    guest_provider_source = (
        "" if guest_provider_node is None else ast.unparse(guest_provider_node)
    )
    required_share_proof = {
        "_GUEST_SHARE_MOUNT_ROOT",
        "(AppleVirtIOFS,",
        "mount.splitlines()",
        "'/bin/test', '-d', os.fspath(_GUEST_SHARE_ROOT)",
    }
    if not all(binding in guest_provider_source for binding in required_share_proof):
        surface_failures.append(
            "guest provider does not separate the VirtioFS mount from its named share"
        )
    executor_source = ast.unparse(executor_tree)
    if "/usr/bin/test" in executor_source or executor_source.count("'/bin/test'") != 5:
        surface_failures.append("macOS guest test executable inventory differs")

    host_launch = function_node(executor_tree, "_spawn_host_only_vm")
    run_calls: list[ast.Call] = []
    for node in ast.walk(executor_tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr if isinstance(node.func, ast.Attribute) else None
        )
        first = node.args[0]
        if (
            function_name in {"_tart_command", "_run_tart"}
            and isinstance(first, ast.Constant)
            and first.value == "run"
        ):
            run_calls.append(node)
    if host_launch is None or len(run_calls) != 1 or run_calls[0] not in set(ast.walk(host_launch)):
        surface_failures.append("Tart has more than one or an unowned VM run path")
    elif [
        item.value for item in run_calls[0].args[:6] if isinstance(item, ast.Constant)
    ] != [
        "run",
        "--no-graphics",
        "--no-audio",
        "--no-clipboard",
        "--net-host",
        "--root-disk-opts=sync=full",
    ]:
        surface_failures.append("Tart run argv differs from strict host-only launch")
    executable_network_tokens = sorted(
        {
            node.value
            for call in run_calls
            for node in call.args
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("--net")
        }
    )
    if executable_network_tokens != ["--net-host"]:
        surface_failures.append("Tart run exposes a non-host-only network token")
    if host_launch is not None:
        host_literals = {
            item.value
            for item in ast.walk(host_launch)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if not {"release:", ":ro", "--dir="}.issubset(host_literals):
            surface_failures.append("Tart qualification share is not AST-bound read-only")
    guest_wait = function_node(executor_tree, "_wait_for_guest_agent")
    retryable_boot_timeout_calls = 0
    if guest_wait is not None:
        retryable_boot_timeout_calls = sum(
            1
            for item in ast.walk(guest_wait)
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "_run_tart"
            and any(
                keyword.arg == "timeout_is_result"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in item.keywords
            )
        )
    if retryable_boot_timeout_calls != 1:
        surface_failures.append(
            "guest-agent boot polling does not own one retryable probe timeout"
        )
    environment_node = function_node(executor_tree, "_tart_environment")
    environment_literals = (
        set()
        if environment_node is None
        else {
            item.value
            for item in ast.walk(environment_node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    )
    if not {"TART_NO_AUTO_PRUNE", "1"}.issubset(environment_literals):
        surface_failures.append("Tart environment does not disable automatic pruning")

    worker_parser = function_node(worker_tree, "_parser")
    worker_options: list[str] = []
    if worker_parser is not None:
        for node in ast.walk(worker_parser):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                worker_options.append(node.args[0].value)
    if worker_options != ["--form", "--launcher", "--attempt-root"]:
        surface_failures.append("installed worker exposes arguments beyond its closed grammar")
    installed_identity = function_node(worker_tree, "_installed_identity")
    installed_literals = (
        set()
        if installed_identity is None
        else {
            item.value
            for item in ast.walk(installed_identity)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    )
    if not {".git", "pyproject.toml", "SOURCE_CHECKOUT_REFUSED"}.issubset(
        installed_literals
    ):
        surface_failures.append("installed worker omits its source-checkout refusal")
    if (
        getattr(worker_module, "WORKER_SCHEMA_ID", None)
        != "KIRBY2_RELEASE_QUALIFICATION_WORKER_RESULT_V1"
        or getattr(worker_module, "EXECUTION_POLICY_ID", None)
        != "KIRBY2_WO40_GH_INSTALLED_EXECUTION_POLICY_V1"
    ):
        surface_failures.append("installed worker schema or execution policy differs")
    surface_case = _case(
        "qualification_executor_and_worker_surfaces_are_closed",
        "The public seams bind one host-only Tart target and one installed-worker grammar.",
        {
            "executor_invocations": 0,
            "executor_signature": observed_executor_signature,
            "provider_digest_refusals": digest_refusals,
            "network_tokens": executable_network_tokens,
            "sequential_clone_loops": sequential_clone_loops,
            "retryable_boot_timeout_calls": retryable_boot_timeout_calls,
            "tart": tart_constants,
            "verifier_signature": observed_verifier_signature,
            "worker_options": worker_options,
            "worker_signature": observed_worker_signature,
        },
        surface_failures,
    )

    cases = (records_case, parser_case, surface_case)
    if len(cases) != DEV0014_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0014 release audit inventory changed")
    return ReleaseAuditSuite(
        "DEV-0014",
        cases,
        metadata=(
            ("executor_invocations", "0"),
            ("interrupted_card", "WO40-G"),
        ),
    )


def audit_release_linux_qualification_executor_restart() -> ReleaseAuditSuite:
    """Prove the WO40-H SSH executor is closed without invoking SSH."""

    repository = _repository_root()
    source_paths = {
        "commands": repository / "kirby2/release/commands.py",
        "executor": repository / "kirby2/release/qualification_executor.py",
        "linux": repository / "kirby2/release/qualification_linux_executor.py",
        "package": repository / "kirby2/release/__init__.py",
        "qualification": repository / "kirby2/release/qualification.py",
        "records": repository / "kirby2/release/qualification_records.py",
        "staging": repository / "kirby2/packs/staging.py",
        "worker": repository / "kirby2/release/qualification_worker.py",
    }
    sources: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    source_failures: list[str] = []
    for source_id, path in source_paths.items():
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            tree = ast.parse(text, filename=os.fspath(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            source_failures.append(
                f"{source_id} source is unavailable: {type(error).__name__}"
            )
            text = ""
            tree = ast.Module(body=[], type_ignores=[])
        sources[source_id] = text
        trees[source_id] = tree

    def function_node(tree: ast.AST, name: str) -> ast.FunctionDef | None:
        return next(
            (
                item
                for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == name
            ),
            None,
        )

    def function_signature(node: ast.FunctionDef | None) -> list[list[object]]:
        if node is None:
            return []
        positional = (*node.args.posonlyargs, *node.args.args)
        optional_start = len(positional) - len(node.args.defaults)
        result = [
            [
                argument.arg,
                (
                    "POSITIONAL_ONLY"
                    if argument in node.args.posonlyargs
                    else "POSITIONAL_OR_KEYWORD"
                ),
                index < optional_start,
            ]
            for index, argument in enumerate(positional)
        ]
        result.extend(
            [argument.arg, "KEYWORD_ONLY", default is None]
            for argument, default in zip(
                node.args.kwonlyargs,
                node.args.kw_defaults,
                strict=True,
            )
        )
        return result

    def assigned_value(tree: ast.Module, name: str) -> ast.expr | None:
        for item in tree.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                targets = (
                    item.targets if isinstance(item, ast.Assign) else (item.target,)
                )
                if any(
                    isinstance(target, ast.Name) and target.id == name
                    for target in targets
                ):
                    return item.value
        return None

    def called_names(node: ast.AST | None) -> set[str]:
        if node is None:
            return set()
        return {
            (
                item.func.id
                if isinstance(item.func, ast.Name)
                else item.func.attr
            )
            for item in ast.walk(node)
            if isinstance(item, ast.Call)
            and isinstance(item.func, (ast.Name, ast.Attribute))
        }

    expected_public_signature = [
        ["bundle", "POSITIONAL_OR_KEYWORD", True],
        ["target_id", "KEYWORD_ONLY", True],
        ["build_evidence", "KEYWORD_ONLY", True],
        ["artifact_root", "KEYWORD_ONLY", True],
    ]
    expected_controller_signature = [
        ["bundle", "POSITIONAL_OR_KEYWORD", True],
        ["build_evidence", "KEYWORD_ONLY", True],
        ["artifact_root", "KEYWORD_ONLY", True],
    ]

    dispatch_failures = list(source_failures)
    executor_tree = trees["executor"]
    linux_tree = trees["linux"]
    public_executor = function_node(executor_tree, "execute_release_qualification")
    mac_executor = function_node(
        executor_tree, "_execute_macos_release_qualification"
    )
    linux_delegate = function_node(
        executor_tree, "_execute_linux_release_qualification"
    )
    linux_executor = function_node(linux_tree, "execute_linux_release_qualification")
    observed_signatures = {
        "linux": function_signature(linux_executor),
        "linux_delegate": function_signature(linux_delegate),
        "macos": function_signature(mac_executor),
        "public": function_signature(public_executor),
    }
    if (
        observed_signatures["public"] != expected_public_signature
        or any(
            observed_signatures[name] != expected_controller_signature
            for name in ("linux", "linux_delegate", "macos")
        )
    ):
        dispatch_failures.append("qualification executor signatures differ")

    dispatch_value = assigned_value(
        executor_tree, "_QUALIFICATION_EXECUTORS_BY_TARGET_V1"
    )
    dispatch_projection: dict[str, str] = {}
    if isinstance(dispatch_value, ast.Dict):
        for key, value in zip(dispatch_value.keys, dispatch_value.values, strict=True):
            key_value: object = None
            if isinstance(key, ast.Constant):
                key_value = key.value
            elif isinstance(key, ast.Name):
                key_assignment = assigned_value(executor_tree, key.id)
                if isinstance(key_assignment, ast.Constant):
                    key_value = key_assignment.value
            if (
                isinstance(key_value, str)
                and isinstance(value, ast.Name)
            ):
                dispatch_projection[key_value] = value.id
    expected_dispatch = {
        "linux-x86_64": "_execute_linux_release_qualification",
        "macos-arm64": "_execute_macos_release_qualification",
    }
    if dispatch_projection != expected_dispatch:
        dispatch_failures.append("qualification target dispatcher differs")
    public_calls = called_names(public_executor)
    if (
        public_executor is None
        or "_QUALIFICATION_EXECUTORS_BY_TARGET_V1" not in {
            item.id
            for item in ast.walk(public_executor)
            if isinstance(item, ast.Name)
        }
        or "_create_clone" in public_calls
        or "_run_form" in public_calls
    ):
        dispatch_failures.append("public qualification wrapper is not dispatch-only")
    if (
        linux_delegate is None
        or "execute_linux_release_qualification" not in called_names(linux_delegate)
    ):
        dispatch_failures.append("Linux dispatcher does not lazily call its closed executor")

    configure = function_node(trees["commands"], "_configure_qualify_release")
    platform_choices: tuple[str, ...] = ()
    if configure is not None:
        for item in ast.walk(configure):
            if not isinstance(item, ast.Call) or not item.args:
                continue
            if not (
                isinstance(item.func, ast.Attribute)
                and item.func.attr == "add_argument"
                and isinstance(item.args[0], ast.Constant)
                and item.args[0].value == "--platform"
            ):
                continue
            choice = next(
                (keyword.value for keyword in item.keywords if keyword.arg == "choices"),
                None,
            )
            if isinstance(choice, (ast.Tuple, ast.List)) and all(
                isinstance(value, ast.Constant) and isinstance(value.value, str)
                for value in choice.elts
            ):
                platform_choices = tuple(value.value for value in choice.elts)  # type: ignore[misc]
    if platform_choices != ("macos-arm64", "linux-x86_64"):
        dispatch_failures.append("qualify-release platform choices differ")
    package_all = assigned_value(trees["package"], "__all__")
    package_exports = (
        tuple(
            item.value
            for item in package_all.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
        if isinstance(package_all, (ast.List, ast.Tuple))
        else ()
    )
    if (
        package_exports.count("execute_release_qualification") != 1
        or "execute_linux_release_qualification" in package_exports
    ):
        dispatch_failures.append("release package executor exports differ")
    worker_policy = assigned_value(trees["worker"], "EXECUTION_POLICY_ID")
    worker_policy_value = (
        worker_policy.value
        if isinstance(worker_policy, ast.Constant)
        and isinstance(worker_policy.value, str)
        else None
    )
    if worker_policy_value != "KIRBY2_WO40_GH_INSTALLED_EXECUTION_POLICY_V1":
        dispatch_failures.append("installed worker policy is not target-neutral")
    dispatch_case = _case(
        "linux_qualification_dispatch_and_contract_are_exact",
        "One public command dispatches exact macOS and Linux executors under one installed-worker policy.",
        {
            "dispatcher": dispatch_projection,
            "executor_invocations": 0,
            "platform_choices": list(platform_choices),
            "signatures": observed_signatures,
            "ssh_invocations": 0,
            "worker_policy": worker_policy_value,
        },
        dispatch_failures,
    )

    transport_failures: list[str] = []
    required_constants = (
        "LINUX_GATE_ID_V1",
        "LINUX_PROVIDER_POLICY_ID_V1",
        "LINUX_TARGET_ID_V1",
        "SFTP_EXECUTABLE_V1",
        "SSH_EXECUTABLE_V1",
        "SSH_HOST_KEY_ALGORITHM_V1",
        "SSH_HOST_KEY_FINGERPRINT_V1",
        "SSH_HOST_KEY_PUBLIC_V1",
        "SSH_HOST_V1",
        "SSH_IDENTITY_FILE_V1",
        "SSH_PORT_V1",
        "SSH_USER_V1",
        "_UNSHARE_PREFIX_V1",
    )
    missing_constants = tuple(
        name for name in required_constants if assigned_value(linux_tree, name) is None
    )
    if missing_constants:
        transport_failures.append("Linux provider constants are incomplete")
    literal_expectations = {
        "LINUX_GATE_ID_V1": "WO40-H",
        "LINUX_PROVIDER_POLICY_ID_V1": "KIRBY2_SSH_EPHEMERAL_HOST_PROVIDER_V1",
        "LINUX_TARGET_ID_V1": "linux-x86_64",
        "SSH_PORT_V1": 22,
    }
    observed_literals: dict[str, object] = {}
    for name, expected in literal_expectations.items():
        value = assigned_value(linux_tree, name)
        observed = value.value if isinstance(value, ast.Constant) else None
        observed_literals[name] = observed
        if observed != expected:
            transport_failures.append(f"Linux provider constant differs: {name}")
    required_helpers = (
        "_remote_cleanup_argv",
        "_remote_root",
        "_remove_local_tree",
        "_require_remote_root",
        "_sftp_argv",
        "_sftp_put",
        "_ssh_argv",
        "_ssh_common_options",
        "_ssh_exec",
    )
    missing_helpers = tuple(
        name for name in required_helpers if function_node(linux_tree, name) is None
    )
    if missing_helpers:
        transport_failures.append("Linux SSH helper inventory is incomplete")
    linux_literals = {
        item.value
        for item in ast.walk(linux_tree)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    required_ssh_literals = {
        "-F",
        "/dev/null",
        "BatchMode=yes",
        "ClearAllForwardings=yes",
        "ForwardAgent=no",
        "ForwardX11=no",
        "IdentityAgent=none",
        "IdentitiesOnly=yes",
        "KbdInteractiveAuthentication=no",
        "PasswordAuthentication=no",
        "PermitLocalCommand=no",
        "ProxyCommand=none",
        "StrictHostKeyChecking=yes",
        "UseKeychain=yes",
        "AddKeysToAgent=no",
    }
    missing_ssh_literals = tuple(sorted(required_ssh_literals - linux_literals))
    if missing_ssh_literals:
        transport_failures.append("Linux SSH option grammar is incomplete")
    linux_source = sources["linux"]
    if (
        "UserKnownHostsFile=" not in linux_source
        or "GUEST_NETWORK_DISABLED_VERIFIED" not in linux_source
        or "SSH_EPHEMERAL_HOST_V1" not in linux_source
    ):
        transport_failures.append("Linux host key, adapter, or network scope is unbound")
    unshare = assigned_value(linux_tree, "_UNSHARE_PREFIX_V1")
    unshare_literals = (
        {
            item.value
            for item in ast.walk(unshare)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if unshare is not None
        else set()
    )
    if not {
        "--fork",
        "--kill-child=KILL",
        "--map-root-user",
        "--net",
        "--pid",
        "--user",
    }.issubset(unshare_literals):
        transport_failures.append("Linux workload lacks its fixed user/network namespace")
    no_new_privileges = assigned_value(
        linux_tree, "_NO_NEW_PRIVILEGES_PREFIX_V1"
    )
    no_new_privileges_literals = (
        {
            item.value
            for item in ast.walk(no_new_privileges)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if no_new_privileges is not None
        else set()
    )
    if not {"/usr/bin/setpriv", "--no-new-privs"}.issubset(
        no_new_privileges_literals
    ):
        transport_failures.append("Linux workload can acquire new process privileges")
    if (
        '"no_new_privileges"' not in linux_source
        or '"home": selected / "home"' not in linux_source
        or '"PIP_NO_CACHE_DIR=1"' not in linux_source
    ):
        transport_failures.append(
            "Linux workload does not prove no-new-privileges or owned HOME confinement"
        )
    staging_source = sources["staging"]
    worker_source = sources["worker"]
    if (
        "_rewind_directory_descriptor" not in staging_source
        or "os.lseek(descriptor, 0, os.SEEK_SET)" not in staging_source
        or staging_source.count("_rewind_directory_descriptor(") < 3
    ):
        transport_failures.append(
            "Linux pack staging can inherit an exhausted duplicated directory cursor"
        )
    executor_source = sources["executor"]
    if (
        "diagnostic = stderr if stderr.strip() else stdout" not in worker_source
        or "_COMMAND_DIAGNOSTIC_MAX_BYTES" not in worker_source
        or "encoded_size + required > maximum_bytes" not in worker_source
        or "encoded[:maximum_bytes].decode(\"utf-8\", errors=\"ignore\")"
        not in worker_source
        or "_validated_worker_failure_fields" not in executor_source
        or "_validated_worker_failure_fields" not in linux_source
        or "_WORKER_FAILURE_DETAIL_MAX_BYTES_V1" not in executor_source
    ):
        transport_failures.append(
            "worker failures can escape the bounded typed outcome grammar"
        )
    if (
        "_PROCESS_ABSENCE_SCRIPT" not in linux_source
        or "/usr/bin/timeout" not in linux_literals
        or "remote provider lock retained" not in linux_source
        or "provider_process_quarantine" not in linux_source
    ):
        transport_failures.append(
            "Linux remote lifetime, process-absence, or quarantine policy is incomplete"
        )
    shell_true_calls = sum(
        1
        for item in ast.walk(linux_tree)
        if isinstance(item, ast.Call)
        and any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in item.keywords
        )
    )
    if shell_true_calls:
        transport_failures.append("Linux qualification enables shell execution")
    forbidden_executables = ("curl", "dnf", "nvidia-smi", "wget", "yum")
    observed_forbidden = tuple(
        sorted(
            executable
            for executable in forbidden_executables
            if any(
                value == executable or value.endswith(f"/{executable}")
                for value in linux_literals
            )
        )
    )
    if observed_forbidden:
        transport_failures.append("Linux qualification exposes download or GPU tools")
    transport_case = _case(
        "linux_provider_transport_and_network_isolation_are_closed",
        "Pinned SSH/SFTP transport stages one provider and executes inside a verified network namespace.",
        {
            "executor_invocations": 0,
            "missing_constants": list(missing_constants),
            "missing_helpers": list(missing_helpers),
            "missing_ssh_options": list(missing_ssh_literals),
            "provider_constants": observed_literals,
            "shell_true_calls": shell_true_calls,
            "ssh_invocations": 0,
        },
        transport_failures,
    )

    hostile_failures: list[str] = []
    hostile_helpers = (
        "_parse_canonical_object_line",
        "_parse_network_probe",
        "_parse_provider_probe",
        "_parse_worker_result",
        "_remote_cleanup_argv",
        "_remote_root",
        "_require_remote_root",
    )
    helper_nodes = {
        name: function_node(linux_tree, name) for name in hostile_helpers
    }
    raise_counts = {
        name: (
            0
            if node is None
            else sum(1 for item in ast.walk(node) if isinstance(item, ast.Raise))
        )
        for name, node in helper_nodes.items()
    }
    required_direct_raises = {
        "_parse_canonical_object_line",
        "_parse_network_probe",
        "_parse_provider_probe",
        "_parse_worker_result",
        "_require_remote_root",
    }
    if any(raise_counts[name] == 0 for name in required_direct_raises):
        hostile_failures.append("Linux hostile parsers omit a direct refusal")
    pure_helper_remote_calls = {
        name: sorted(called_names(node) & {"_sftp_put", "_ssh_exec"})
        for name, node in helper_nodes.items()
    }
    if any(pure_helper_remote_calls.values()):
        hostile_failures.append("Linux hostile parser or path helper invokes a provider")
    cleanup_node = helper_nodes["_remote_cleanup_argv"]
    if "_require_remote_root" not in called_names(cleanup_node):
        hostile_failures.append("Linux cleanup argv does not revalidate its owned root")
    cleanup_value = assigned_value(linux_tree, "_CLEANUP_SCRIPT")
    cleanup_script = (
        cleanup_value.value
        if isinstance(cleanup_value, ast.Constant)
        and isinstance(cleanup_value.value, str)
        else ""
    )
    cleanup_tokens = {
        "descriptor rewind": "os.lseek(descriptor,0,os.SEEK_SET)",
        "directory no-follow open": 'getattr(os,"O_NOFOLLOW",0)',
        "marker descriptor revalidation": "file_identity(os.fstat(marker_fd))",
        "marker-only root proof": "directory_entries(directory)!=[marker_name]",
        "owned directory opening": "def open_owned_directory(parent_fd,child)",
        "owned directory permission repair": "os.fchmod(descriptor,0o700)",
        "same-device confinement": "metadata.st_dev!=root_device",
        "same-owner confinement": "metadata.st_uid!=owner",
        "symlink-safe stat": "follow_symlinks=False",
    }
    missing_cleanup_tokens = tuple(
        sorted(
            label
            for label, token in cleanup_tokens.items()
            if token not in cleanup_script
        )
    )
    child_delete = cleanup_script.find("if child!=marker_name: delete_node")
    marker_revalidation = cleanup_script.find("named_marker=os.stat(marker_name")
    marker_delete = cleanup_script.find(
        "os.unlink(marker_name,dir_fd=directory)"
    )
    marker_close = cleanup_script.find("os.close(marker_fd)")
    if (
        missing_cleanup_tokens
        or min(child_delete, marker_revalidation, marker_delete, marker_close) < 0
        or not child_delete < marker_revalidation < marker_delete < marker_close
        or "shutil.rmtree" in cleanup_script
    ):
        hostile_failures.append(
            "Linux cleanup is not descriptor-confined with marker-last ownership"
        )
    publication_node = function_node(executor_tree, "_publish_immutable_file")
    publication_patterns = (
        ()
        if publication_node is None
        else tuple(
            item.value
            for item in ast.walk(publication_node)
            if isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and "\\.json" in item.value
        )
    )
    frozen_record_names = (
        "clean-provider-macos-arm64.json",
        "clean-provider-linux-x86_64.json",
        "qualification-attempt.json",
    )
    if len(publication_patterns) != 1 or any(
        re.fullmatch(publication_patterns[0], name) is None
        for name in frozen_record_names
    ):
        hostile_failures.append(
            "immutable publication grammar rejects a frozen qualification record name"
        )
    refusal_attributes = {
        item.attr
        for item in ast.walk(linux_tree)
        if isinstance(item, ast.Attribute)
    }
    required_refusals = {
        "PROVIDER_CLEANUP_FAILED",
        "PROVIDER_IDENTITY_MISMATCH",
        "PROVIDER_ISOLATION_UNAVAILABLE",
        "PROVIDER_NOT_CLEAN",
        "RESULT_INVALID",
    }
    missing_refusals = tuple(sorted(required_refusals - refusal_attributes))
    if missing_refusals:
        hostile_failures.append("Linux typed refusal inventory is incomplete")
    linux_entry = function_node(linux_tree, "execute_linux_release_qualification")
    final_cleanup_calls: set[str] = set()
    if linux_entry is not None:
        for item in ast.walk(linux_entry):
            if not isinstance(item, ast.Try) or not item.finalbody:
                continue
            final_cleanup_calls.update(
                called_names(ast.Module(body=item.finalbody, type_ignores=[]))
            )
    if not any("cleanup" in name for name in final_cleanup_calls):
        hostile_failures.append("Linux executor lacks a finally-owned cleanup call")
    if "_remote_cleanup_argv" not in called_names(linux_tree):
        hostile_failures.append("Linux executor never uses the closed cleanup grammar")
    entry_call_lines: dict[str, list[int]] = {}
    if linux_entry is not None:
        for item in ast.walk(linux_entry):
            if not isinstance(item, ast.Call) or not isinstance(
                item.func, (ast.Name, ast.Attribute)
            ):
                continue
            name = (
                item.func.id
                if isinstance(item.func, ast.Name)
                else item.func.attr
            )
            entry_call_lines.setdefault(name, []).append(item.lineno)
    cleanup_lines = entry_call_lines.get("_cleanup_remote_root", [])
    publication_lines = entry_call_lines.get("_publish_records", [])
    if (
        not cleanup_lines
        or not publication_lines
        or min(cleanup_lines) >= min(publication_lines)
    ):
        hostile_failures.append("Linux evidence publication can precede remote cleanup")
    local_retirement_offset = linux_source.find("provider_local_retired = True")
    candidate_verification_offset = linux_source.find(
        "candidate_provider_raw=provider_record.canonical_bytes()"
    )
    publication_offset = linux_source.find("        _publish_records(")
    if not (
        0 <= local_retirement_offset
        < candidate_verification_offset
        < publication_offset
    ):
        hostile_failures.append(
            "Linux immutable publication precedes local retirement or candidate verification"
        )
    hostile_case = _case(
        "linux_hostile_results_and_cleanup_fail_closed",
        "Canonical parsers, owned-root validation, typed refusals, and finally cleanup remain provider-free under audit.",
        {
            "executor_invocations": 0,
            "missing_cleanup_contracts": list(missing_cleanup_tokens),
            "missing_refusals": list(missing_refusals),
            "parser_raise_counts": raise_counts,
            "publication_patterns": list(publication_patterns),
            "pure_helper_remote_calls": pure_helper_remote_calls,
            "ssh_invocations": 0,
        },
        hostile_failures,
    )

    baseline_failures: list[str] = []
    baseline_node = function_node(
        trees["qualification"], "_require_macos_integer_core_baseline"
    )
    baseline_source = "" if baseline_node is None else ast.unparse(baseline_node)
    required_baseline_tokens = {
        "artifact_index_sha256",
        "build_evidence_sha256",
        "candidate_commit",
        "cross_platform_integer_core_sha256",
        "protocol_set_sha256",
        "source_manifest_sha256",
    }
    missing_baseline_tokens = tuple(
        sorted(token for token in required_baseline_tokens if token not in baseline_source)
    )
    baseline_calls = called_names(baseline_node)
    if baseline_node is None:
        baseline_failures.append("macOS integer-core baseline helper is missing")
    if missing_baseline_tokens:
        baseline_failures.append("macOS baseline identity binding is incomplete")
    if not {
        "release_qualification_record_paths",
        "verify_release_qualification_record",
    }.issubset(baseline_calls):
        baseline_failures.append("macOS baseline does not reparse and verify qualification records")
    from_bytes_calls = sum(
        1
        for item in ast.walk(baseline_node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "from_bytes"
    ) if baseline_node is not None else 0
    if from_bytes_calls < 2 or "PASS" not in baseline_source:
        baseline_failures.append("macOS baseline does not require two passing typed records")
    forbidden_baseline_calls = baseline_calls & {
        "_run_tart",
        "_ssh_exec",
        "execute_release_qualification",
    }
    if forbidden_baseline_calls:
        baseline_failures.append("macOS baseline helper invokes a provider")
    deep_node = function_node(
        trees["qualification"], "_verify_release_qualification_records"
    )
    deep_source = "" if deep_node is None else ast.unparse(deep_node)
    if (
        "_require_macos_integer_core_baseline" not in called_names(deep_node)
        or "linux-x86_64" not in deep_source
        or "candidate_provider_raw" not in deep_source
        or "candidate_attempt_raw" not in deep_source
    ):
        baseline_failures.append(
            "deep Linux verification omits the macOS baseline or candidate mode"
        )
    baseline_lines = entry_call_lines.get(
        "_require_macos_integer_core_baseline", []
    )
    if (
        not baseline_lines
        or not publication_lines
        or min(baseline_lines) >= min(publication_lines)
    ):
        baseline_failures.append("Linux executor does not bind the baseline before publication")
    baseline_case = _case(
        "linux_cross_platform_baseline_is_independently_bound",
        "Provider-free verification reparses WO40-G and binds every shared identity before Linux publication.",
        {
            "baseline_from_bytes_calls": from_bytes_calls,
            "baseline_tokens": sorted(required_baseline_tokens - set(missing_baseline_tokens)),
            "executor_invocations": 0,
            "forbidden_provider_calls": sorted(forbidden_baseline_calls),
            "ssh_invocations": 0,
        },
        baseline_failures,
    )

    cases = (
        dispatch_case,
        transport_case,
        hostile_case,
        baseline_case,
    )
    if len(cases) != DEV0015_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0015 release audit inventory changed")
    return ReleaseAuditSuite(
        "DEV-0015",
        cases,
        metadata=(
            ("executor_invocations", "0"),
            ("interrupted_card", "WO40-H"),
            ("ssh_invocations", "0"),
        ),
    )


def _git_tracks_clean_working_file(repository: Path, relative: str) -> tuple[bool, str]:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        return False, "evidence document is not staged or tracked"
    unstaged = subprocess.run(
        ["git", "diff", "--quiet", "--", relative],
        cwd=repository,
        check=False,
    )
    if unstaged.returncode != 0:
        return False, "evidence document has unstaged changes"
    return True, "evidence document is present in the Git index"


def _load_future_evidence(repository: Path, gate_id: str) -> ReleaseGateEvidenceV1:
    relative = RELEASE_FUTURE_EVIDENCE_PATHS_V1[gate_id]
    return parse_release_gate_evidence_markdown((repository / relative).read_bytes())


def audit_release_frozen_evidence(gate_id: str) -> ReleaseAuditSuite:
    """Verify one predeclared F-I document without executing its one-time work."""

    from kirby2.release.build import (
        ReleaseCommandStatusV1,
        load_release_protocol_bundle,
        verify_release_artifacts,
    )
    from kirby2.release.performance import RunnerSourceTreeV1

    if gate_id not in RELEASE_FUTURE_EVIDENCE_PATHS_V1:
        raise ValueError("future release evidence gate is not preregistered")
    repository = _repository_root()
    relative = RELEASE_FUTURE_EVIDENCE_PATHS_V1[gate_id]
    document = repository / relative
    if not document.is_file():
        return ReleaseAuditSuite(
            gate_id,
            (
                _not_exercised_case(
                    "immutable_release_evidence_exists",
                    "The preregistered validator does not execute missing release work.",
                    relative,
                ),
            ),
            metadata=(("evidence_path", relative),),
        )

    failures: list[str] = []
    warnings: list[str] = []
    try:
        evidence = _load_future_evidence(repository, gate_id)
    except (OSError, TypeError, ValueError) as error:
        return ReleaseAuditSuite(
            gate_id,
            (
                _case(
                    "immutable_release_evidence_verifies",
                    "Present release evidence must satisfy the frozen canonical contract.",
                    {"evidence_path": relative},
                    (f"evidence parse failed: {type(error).__name__}",),
                ),
            ),
            metadata=(("evidence_path", relative),),
        )
    if evidence.gate_id != gate_id:
        failures.append("evidence document names another gate")
    tracked, tracked_detail = _git_tracks_clean_working_file(repository, relative)
    if not tracked:
        failures.append(tracked_detail)

    bundle = load_release_protocol_bundle(repository)
    if evidence.protocol_set_sha256 != bundle.protocol_set_sha256:
        failures.append("evidence protocol-set digest differs from the frozen protocol")
    source_tree = RunnerSourceTreeV1.from_bytes(
        (repository / "release/performance_runner_sources.lock").read_bytes()
    )
    if evidence.source_manifest_sha256 != source_tree.source_manifest_sha256:
        failures.append("evidence source-manifest digest differs from the candidate lock")

    referenced: list[list[str]] = []
    for row in evidence.evidence_records:
        relative_record = Path(str(row["path"]))
        selected = (repository / relative_record).resolve(strict=False)
        try:
            selected.relative_to(repository)
        except ValueError:
            failures.append(f"evidence record escapes repository: {row['evidence_id']}")
            continue
        if not selected.is_file():
            failures.append(f"evidence record is missing: {row['evidence_id']}")
            continue
        raw = selected.read_bytes()
        if len(raw) != row["size"] or _sha256(raw) != row["sha256"]:
            failures.append(f"evidence record identity differs: {row['evidence_id']}")
        referenced.append([str(row["evidence_id"]), str(row["sha256"])])

    build_evidence: ReleaseGateEvidenceV1 | None = None
    if gate_id == "WO40-F":
        from kirby2.release.artifacts import ReleaseArtifactBuildRecordV1

        artifact_root = repository / ".kirby2/release"
        artifact_verification = verify_release_artifacts(
            bundle,
            artifact_root,
            candidate_commit=evidence.candidate_commit,
        )
        if artifact_verification.status is not ReleaseCommandStatusV1.PASS:
            failures.append("immutable release artifact verification did not pass")
        index_path = artifact_root / "release-artifact-index.json"
        if not index_path.is_file() or _sha256(index_path.read_bytes()) != evidence.artifact_index_sha256:
            failures.append("build evidence artifact-index digest differs")
        if artifact_verification.payload.get("candidate_commit") != evidence.candidate_commit:
            failures.append("build evidence candidate differs from artifact index")
        records_by_id = {
            str(row["evidence_id"]): row for row in evidence.evidence_records
        }
        if set(records_by_id) != {"artifact-index", "release-build-record"}:
            failures.append("build evidence must reference exactly the index and build record")
        else:
            expected_paths = {
                "artifact-index": ".kirby2/release/release-artifact-index.json",
                "release-build-record": ".kirby2/release/release-build-record.json",
            }
            if any(
                records_by_id[evidence_id]["path"] != path
                for evidence_id, path in expected_paths.items()
            ):
                failures.append("build evidence record paths differ from the governed store")
            record_path = artifact_root / "release-build-record.json"
            try:
                record = ReleaseArtifactBuildRecordV1.from_bytes(
                    record_path.read_bytes()
                )
            except (OSError, TypeError, ValueError) as error:
                failures.append(
                    f"release build record failed parsing: {type(error).__name__}"
                )
            else:
                if record.candidate_commit != evidence.candidate_commit:
                    failures.append("release build record candidate differs")
                if record.artifact_index_sha256 != evidence.artifact_index_sha256:
                    failures.append("release build record artifact index differs")
                if tuple(item.as_dict() for item in record.checks) != evidence.checks:
                    failures.append("release build record check bindings differ")
    else:
        build_path = repository / RELEASE_FUTURE_EVIDENCE_PATHS_V1["WO40-F"]
        if not build_path.is_file():
            failures.append("upstream immutable build evidence is missing")
        else:
            try:
                build_evidence = _load_future_evidence(repository, "WO40-F")
            except (OSError, TypeError, ValueError) as error:
                failures.append(
                    f"upstream build evidence failed parsing: {type(error).__name__}"
                )
            if build_evidence is not None and (
                evidence.candidate_commit != build_evidence.candidate_commit
                or evidence.artifact_index_sha256
                != build_evidence.artifact_index_sha256
                or evidence.source_manifest_sha256
                != build_evidence.source_manifest_sha256
            ):
                failures.append("evidence identity differs from immutable build evidence")

        if gate_id in {"WO40-G", "WO40-H"} and build_path.is_file():
            from kirby2.release.qualification import verify_release_qualification
            from kirby2.release.qualification_records import (
                ReleaseQualificationAttemptV1,
                release_qualification_record_paths,
            )

            target_id = "macos-arm64" if gate_id == "WO40-G" else "linux-x86_64"
            provider_relative, attempt_relative = release_qualification_record_paths(
                target_id
            )
            expected_records = {
                "provider-attestation": f".kirby2/release/{provider_relative}",
                "qualification-attempt": f".kirby2/release/{attempt_relative}",
            }
            records_by_id = {
                str(row["evidence_id"]): row for row in evidence.evidence_records
            }
            if set(records_by_id) != set(expected_records) or any(
                records_by_id[evidence_id]["path"] != path
                for evidence_id, path in expected_records.items()
                if evidence_id in records_by_id
            ):
                failures.append(
                    "platform evidence must reference exactly the provider and attempt"
                )
            try:
                deep = verify_release_qualification(
                    bundle,
                    target_id=target_id,
                    build_evidence=build_path.resolve(strict=True),
                    artifact_root=(repository / ".kirby2/release").resolve(strict=True),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                failures.append(
                    f"deep qualification verification failed: {type(error).__name__}"
                )
            else:
                attempt_path = repository / ".kirby2/release" / attempt_relative
                try:
                    attempt = ReleaseQualificationAttemptV1.from_bytes(
                        attempt_path.read_bytes()
                    )
                except (OSError, TypeError, ValueError) as error:
                    failures.append(
                        f"qualification attempt failed parsing: {type(error).__name__}"
                    )
                else:
                    if tuple(
                        item.as_dict() for item in attempt.checks
                    ) != evidence.checks:
                        failures.append(
                            "platform evidence checks differ from the reconstructed attempt"
                        )
                    if attempt.facts.as_dict() != evidence.facts:
                        failures.append(
                            "platform evidence facts differ from the reconstructed attempt"
                        )
                    if deep.status != evidence.status:
                        failures.append(
                            "platform evidence status differs from the deep verification"
                        )
                    expected_digests = {
                        "provider-attestation": deep.provider_attestation_sha256,
                        "qualification-attempt": deep.qualification_attempt_sha256,
                    }
                    if any(
                        records_by_id[evidence_id]["sha256"] != digest
                        for evidence_id, digest in expected_digests.items()
                        if evidence_id in records_by_id
                    ):
                        failures.append(
                            "platform evidence record digests differ from deep verification"
                        )

    if gate_id == "WO40-H":
        macos_path = repository / RELEASE_FUTURE_EVIDENCE_PATHS_V1["WO40-G"]
        if not macos_path.is_file():
            failures.append("macOS cross-platform baseline evidence is missing")
        else:
            try:
                macos = _load_future_evidence(repository, "WO40-G")
            except (OSError, TypeError, ValueError) as error:
                failures.append(f"macOS evidence failed parsing: {type(error).__name__}")
            else:
                if (
                    macos.facts["cross_platform_integer_core_sha256"]
                    != evidence.facts["cross_platform_integer_core_sha256"]
                ):
                    failures.append("cross-platform integer-core identity differs")

    if evidence.status not in {"PASS", "PASS_WITH_WARNINGS"}:
        failures.append(f"evidence terminal status is {evidence.status}")
    elif evidence.status == "PASS_WITH_WARNINGS":
        warnings.append("release evidence contains preregistered warning results")

    audit_case = ReleaseAuditCase(
        name="immutable_release_evidence_verifies",
        detail="Committed evidence, referenced records, candidate, protocol, and source lock agree.",
        evidence={
            "artifact_index_sha256": evidence.artifact_index_sha256,
            "candidate_commit": evidence.candidate_commit,
            "document_sha256": _sha256(document.read_bytes()),
            "referenced_records": referenced,
        },
        failures=tuple(failures),
        warnings=tuple(warnings) if not failures else (),
    )
    return ReleaseAuditSuite(
        gate_id,
        (audit_case,),
        metadata=(
            ("evidence_path", relative),
            ("evidence_schema", RELEASE_GATE_EVIDENCE_SCHEMA_ID_V1),
        ),
    )


def audit_release_build_evidence() -> ReleaseAuditSuite:
    return audit_release_frozen_evidence("WO40-F")


def audit_release_macos_evidence() -> ReleaseAuditSuite:
    return audit_release_frozen_evidence("WO40-G")


def audit_release_linux_evidence() -> ReleaseAuditSuite:
    return audit_release_frozen_evidence("WO40-H")


def audit_release_performance_evidence() -> ReleaseAuditSuite:
    return audit_release_frozen_evidence("WO40-I")


def _publish_immutable(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_bytes() != raw:
            raise RuntimeError(f"immutable release evidence conflicts: {path.name}")
        return
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"immutable release evidence target is unsafe: {path.name}")
    descriptor, temporary_name = mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def publish_release_closeout_prerequisites(
    repository: Path,
    reports: Iterable[tuple[str, bytes, str]],
) -> Path:
    """Publish exact prior-gate bytes only after the aggregate already passed."""

    from kirby2.release.qualification import (
        WO40_J_PREREQUISITES_ID_V1,
        WO40_J_REQUIRED_PRIOR_GATES_V1,
        ReleaseEvidenceReferenceV1,
        verify_closeout_prerequisites,
    )

    if not isinstance(repository, Path) or not repository.is_absolute():
        raise ValueError("release prerequisite repository must be absolute")
    rows = tuple(reports)
    by_gate = {gate_id: (raw, status) for gate_id, raw, status in rows}
    if len(by_gate) != len(rows):
        raise ValueError("release prerequisite reports contain duplicate gates")
    required = set(WO40_J_REQUIRED_PRIOR_GATES_V1) | set(
        RELEASE_REQUIRED_DEVIATION_GATE_IDS_V1
    )
    if set(by_gate) != required:
        raise ValueError("release prerequisite report inventory differs")
    allowed_statuses = {"PASS", "PASS_WITH_WARNINGS"}
    if any(status not in allowed_statuses for _, status in by_gate.values()):
        raise ValueError("release prerequisite publication requires passing reports")

    evidence_root = repository / ".kirby2/release/gate-evidence"
    reference_rows: list[dict[str, object]] = []
    deviation_rows: list[dict[str, object]] = []
    typed_references = []
    for gate_id in (
        *WO40_J_REQUIRED_PRIOR_GATES_V1,
        *RELEASE_REQUIRED_DEVIATION_GATE_IDS_V1,
    ):
        raw, status = by_gate[gate_id]
        relative = f".kirby2/release/gate-evidence/{gate_id}.json"
        path = evidence_root / f"{gate_id}.json"
        _publish_immutable(path, raw)
        row = {
            "evidence_id": relative,
            "gate_id": gate_id,
            "sha256": _sha256(raw),
            "size": len(raw),
            "status": status,
        }
        if gate_id.startswith("DEV-"):
            deviation_rows.append(row)
        else:
            reference_rows.append(row)
            typed_references.append(ReleaseEvidenceReferenceV1(**row))
    verification = verify_closeout_prerequisites(tuple(typed_references))
    if verification["status"] != "PASS":
        raise RuntimeError("release closeout prerequisite projection did not pass")
    packet = {
        "deviation_references": deviation_rows,
        "prerequisite_id": WO40_J_PREREQUISITES_ID_V1,
        "references": reference_rows,
        "schema_id": RELEASE_CLOSEOUT_PREREQUISITE_PACKET_SCHEMA_ID_V1,
        "schema_version": 1,
        "status": "PASS",
        "verification": verification,
    }
    path = repository / ".kirby2/release/closeout-prerequisites.json"
    _publish_immutable(path, canonical_json_bytes(packet))
    return path


def _verify_prerequisite_reference(
    repository: Path,
    row: object,
) -> tuple[str, str, str]:
    if type(row) is not dict or set(row) != {
        "evidence_id",
        "gate_id",
        "sha256",
        "size",
        "status",
    }:
        raise ValueError("release prerequisite reference fields differ")
    gate_id = row["gate_id"]
    evidence_id = row["evidence_id"]
    digest = row["sha256"]
    size = row["size"]
    status = row["status"]
    if (
        type(gate_id) is not str
        or type(evidence_id) is not str
        or type(digest) is not str
        or type(size) is not int
        or type(status) is not str
        or size <= 0
        or _SHA256.fullmatch(digest) is None
        or status not in {"PASS", "PASS_WITH_WARNINGS"}
    ):
        raise ValueError("release prerequisite reference values differ")
    relative = Path(evidence_id)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("release prerequisite evidence path is unsafe")
    path = (repository / relative).resolve(strict=False)
    try:
        path.relative_to(repository)
    except ValueError as error:
        raise ValueError("release prerequisite evidence escapes repository") from error
    raw = path.read_bytes()
    if len(raw) != size or _sha256(raw) != digest:
        raise ValueError("release prerequisite evidence identity differs")
    report = load_canonical_json_bytes(raw, "release expansion gate report")
    if type(report) is not dict or report.get("card_id") != gate_id or report.get(
        "status"
    ) != status:
        raise ValueError("release prerequisite report content differs")
    return gate_id, digest, status


def audit_release_closeout_prerequisites() -> ReleaseAuditSuite:
    """Verify the non-self-referential WO40-J prerequisite packet."""

    from kirby2.release.qualification import (
        WO40_J_PREREQUISITES_ID_V1,
        WO40_J_REQUIRED_PRIOR_GATES_V1,
        ReleaseEvidenceReferenceV1,
        verify_closeout_prerequisites,
    )

    repository = _repository_root()
    relative = ".kirby2/release/closeout-prerequisites.json"
    path = repository / relative
    if not path.is_file():
        return ReleaseAuditSuite(
            "WO40-J",
            (
                _not_exercised_case(
                    "closeout_prerequisite_packet_verifies",
                    "The closeout gate awaits a fully passing aggregate and creates no self-evidence.",
                    relative,
                ),
            ),
            metadata=(("prerequisite_id", WO40_J_PREREQUISITES_ID_V1),),
        )
    failures: list[str] = []
    references: tuple[object, ...] = ()
    deviations: tuple[object, ...] = ()
    verification: object = None
    try:
        payload = load_canonical_json_bytes(path.read_bytes(), "release prerequisite packet")
        if type(payload) is not dict or set(payload) != {
            "deviation_references",
            "prerequisite_id",
            "references",
            "schema_id",
            "schema_version",
            "status",
            "verification",
        }:
            raise ValueError("release prerequisite packet fields differ")
        if (
            payload["schema_id"]
            != RELEASE_CLOSEOUT_PREREQUISITE_PACKET_SCHEMA_ID_V1
            or payload["schema_version"] != 1
            or payload["prerequisite_id"] != WO40_J_PREREQUISITES_ID_V1
            or payload["status"] != "PASS"
            or type(payload["references"]) is not list
            or type(payload["deviation_references"]) is not list
        ):
            raise ValueError("release prerequisite packet identity differs")
        references = tuple(payload["references"])
        deviations = tuple(payload["deviation_references"])
        canonical_ids = tuple(
            _verify_prerequisite_reference(repository, row)[0] for row in references
        )
        deviation_ids = tuple(
            _verify_prerequisite_reference(repository, row)[0] for row in deviations
        )
        if canonical_ids != WO40_J_REQUIRED_PRIOR_GATES_V1:
            raise ValueError("release prerequisite canonical gate order differs")
        if deviation_ids != RELEASE_REQUIRED_DEVIATION_GATE_IDS_V1:
            raise ValueError("release prerequisite deviation gate order differs")
        typed = tuple(ReleaseEvidenceReferenceV1(**row) for row in references)
        verification = verify_closeout_prerequisites(typed)
        if payload["verification"] != verification or verification["status"] != "PASS":
            raise ValueError("release prerequisite verification projection differs")
    except (OSError, TypeError, ValueError) as error:
        failures.append(f"prerequisite packet failed: {type(error).__name__}")
    case = _case(
        "closeout_prerequisite_packet_verifies",
        "Every prior canonical/deviation report verifies without referencing WO40-J itself.",
        {
            "canonical_reference_count": len(references),
            "deviation_reference_count": len(deviations),
            "packet_sha256": _sha256(path.read_bytes()),
            "verification": verification,
        },
        failures,
    )
    return ReleaseAuditSuite(
        "WO40-J",
        (case,),
        metadata=(("prerequisite_id", WO40_J_PREREQUISITES_ID_V1),),
    )


def audit_release_frontier_registration() -> ReleaseAuditSuite:
    """Focused DEV-0009 proof for the recovered release audit frontier."""

    callable_names = (
        audit_release_data_and_migrations,
        audit_release_recovery,
        audit_release_backup_restore,
        audit_release_first_run,
        audit_release_protocol,
        audit_release_resource_report,
        audit_release_candidate_source,
        audit_release_build_evidence,
        audit_release_macos_evidence,
        audit_release_linux_evidence,
        audit_release_performance_evidence,
        audit_release_closeout_prerequisites,
    )
    inventory_failures: list[str] = []
    if len(callable_names) != 12 or any(not callable(item) for item in callable_names):
        inventory_failures.append("release audit callable inventory differs")
    inventory_case = _case(
        "wo40_release_audit_frontier_is_complete",
        "A-E, D1, F-I, and the non-self-referential J prerequisite gate are callable.",
        {"callable_count": len(callable_names)},
        inventory_failures,
    )

    evidence_failures: list[str] = []
    digests: list[list[str]] = []
    for gate_id in RELEASE_FUTURE_EVIDENCE_PATHS_V1:
        fixture = _synthetic_future_evidence(gate_id)
        restored = parse_release_gate_evidence_markdown(
            render_release_gate_evidence_markdown(fixture).encode("utf-8")
        )
        if restored != fixture:
            evidence_failures.append(f"{gate_id} evidence contract did not round-trip")
        digests.append([gate_id, _sha256(fixture.canonical_bytes())])
    evidence_case = _case(
        "frozen_future_evidence_contracts_round_trip",
        "Every evidence-only card has one strict preregistered canonical envelope.",
        {"fixtures": digests},
        evidence_failures,
    )

    missing_failures: list[str] = []
    with TemporaryDirectory(prefix="kirby2-release-frontier-") as temporary:
        missing = Path(temporary).resolve()
        for gate_id, relative in RELEASE_FUTURE_EVIDENCE_PATHS_V1.items():
            if (missing / relative).exists():
                missing_failures.append(f"synthetic missing fixture unexpectedly exists: {gate_id}")
    # The real future gates are intentionally not invoked here: an existing one-time
    # result must never be mistaken for a synthetic fixture or rerun by DEV-0009.
    missing_case = _case(
        "future_work_is_not_executed_by_registration",
        "DEV-0009 registers validators only and does not create F-J evidence or artifacts.",
        {"future_gate_count": len(RELEASE_FUTURE_EVIDENCE_PATHS_V1) + 1},
        missing_failures,
    )

    cases = (inventory_case, evidence_case, missing_case)
    if len(cases) != DEV0009_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0009 release audit inventory changed")
    return ReleaseAuditSuite(
        "DEV-0009",
        cases,
        metadata=(("interrupted_card", "WO40-E"),),
    )
