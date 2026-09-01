"""Closed installed-artifact worker for WO40-G/H clean-environment qualification.

The worker deliberately accepts no command payload.  Its three CLI arguments select
one frozen product form, one already-installed launcher, and one new disposable
attempt root.  Every child argv is constructed below, network-related environment
state is disabled, stdout is retained for exact verification, and the public stdout
contract is one canonical JSON object followed by one line feed.

This module is shipped in the wheel because several qualification seams need to run
after the source tree is gone: saved-run microscope binding, replay-pack execution
from the installed registry, crash recovery, calibrated-profile reload, and an exact
one-unit distributed-worker check.  Those helpers remain offline and confined to the
new attempt root.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.resources
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes


WORKER_SCHEMA_ID: Final = "KIRBY2_RELEASE_QUALIFICATION_WORKER_RESULT_V1"
STEP_SCHEMA_ID: Final = "KIRBY2_RELEASE_QUALIFICATION_STEP_RESULT_V1"
OBSERVATION_SCHEMA_ID: Final = "KIRBY2_RELEASE_COMMAND_OBSERVATION_V1"
EXECUTION_POLICY_ID: Final = "KIRBY2_WO40_GH_INSTALLED_EXECUTION_POLICY_V1"
OFFLINE_POLICY_ID: Final = "KIRBY2_RELEASE_OFFLINE_SUBPROCESS_POLICY_V1"
MICROSCOPE_RECEIPT_ID: Final = "KIRBY2_SAVED_RUN_MICROSCOPE_RECEIPT_V1"
IMPORTED_REPLAY_RECEIPT_ID: Final = "KIRBY2_IMPORTED_REPLAY_EXECUTION_RECEIPT_V1"
CRASH_RECOVERY_RECEIPT_ID: Final = "KIRBY2_API_CRASH_RECOVERY_RECEIPT_V1"
CALIBRATION_RECEIPT_ID: Final = "KIRBY2_CALIBRATION_RELOAD_RECEIPT_V1"
DISTRIBUTED_RECEIPT_ID: Final = "KIRBY2_ONE_UNIT_DISTRIBUTED_RECEIPT_V1"

_FUNCTIONAL_ORDER: Final = (
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
_HEADLESS_ORDER: Final = (
    "HEADLESS_SIMULATION",
    "HEADLESS_AUDIT",
    "HEADLESS_CALIBRATION",
    "HEADLESS_DISTRIBUTED_WORKER",
)
_ROOT_ROLE_BY_STEP: Final = {
    "CLEAN_INSTALL": "PRIMARY_CLEAN_ROOT",
    "LAUNCH": "PRIMARY_CLEAN_ROOT",
    "FULL_FIRST_RUN": "PRIMARY_CLEAN_ROOT",
    "STARTER_LESSON": "PRIMARY_CLEAN_ROOT",
    "PLACE_CANCEL": "PRIMARY_CLEAN_ROOT",
    "COMPLETE_SAVE": "PRIMARY_CLEAN_ROOT",
    "OPEN_REPLAY_MICROSCOPE": "PRIMARY_CLEAN_ROOT",
    "EXPORT_PACK": "PRIMARY_CLEAN_ROOT",
    "CLOSE": "PRIMARY_CLEAN_ROOT",
    "REOPEN_VERIFY_SAVED": "PRIMARY_CLEAN_ROOT",
    "IMPORT_SECOND_CLEAN_ROOT": "SECONDARY_CLEAN_ROOT",
    "REPLAY_IMPORTED_LESSON": "SECONDARY_CLEAN_ROOT",
    "COMPARE_DECLARED_REPLAY_DIGEST": "BOTH_CLEAN_ROOTS",
    "RESTORE_BACKUP": "RESTORE_CLEAN_ROOT",
    "CRASH_RECOVERY": "PRIMARY_CLEAN_ROOT",
    "EXPORT_DIAGNOSTICS": "PRIMARY_CLEAN_ROOT",
    "UNINSTALL_PRESERVE_USER_DATA": "PRIMARY_CLEAN_ROOT",
    "HEADLESS_SIMULATION": "PRIMARY_CLEAN_ROOT",
    "HEADLESS_AUDIT": "PRIMARY_CLEAN_ROOT",
    "HEADLESS_CALIBRATION": "PRIMARY_CLEAN_ROOT",
    "HEADLESS_DISTRIBUTED_WORKER": "PRIMARY_CLEAN_ROOT",
}
_MAX_CAPTURE_BYTES: Final = 16 * 1024 * 1024
_COMMAND_TIMEOUT_SECONDS: Final = 240
_COMMAND_DIAGNOSTIC_MAX_BYTES: Final = 2048
_FAILURE_CODE_MAX_BYTES: Final = 128
_FAILURE_DETAIL_MAX_BYTES: Final = 3072
_SHA256_EMPTY: Final = hashlib.sha256(b"").hexdigest()


class QualificationFailure(RuntimeError):
    """One closed worker refusal or failed product expectation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = _safe_text(code, _FAILURE_CODE_MAX_BYTES)
        self.detail = _safe_text(detail, _FAILURE_DETAIL_MAX_BYTES)
        super().__init__(f"{self.code}: {self.detail}")


class _ArgumentsRefused(ValueError):
    """Parser refusal that does not leak argparse prose to stderr."""


class _ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentsRefused(message)


@dataclass(slots=True)
class _CommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    observation: dict[str, object]


@dataclass(slots=True)
class _Worker:
    form: str
    launcher: Path
    attempt_root: Path
    step_results: dict[str, dict[str, object]] = field(default_factory=dict)
    observations: list[dict[str, object]] = field(default_factory=list)
    command_ids_by_step: dict[str, list[str]] = field(default_factory=dict)
    execution_index: int = 0
    run_id: str | None = None
    declared_replay_digest: str | None = None
    replay_pack_id: str | None = None
    replay_pack: Path | None = None
    cross_platform_integer_core_sha256: str | None = None

    @property
    def target_id(self) -> str:
        observed = (platform.system(), platform.machine())
        if observed == ("Darwin", "arm64"):
            return "macos-arm64"
        if observed == ("Linux", "x86_64"):
            return "linux-x86_64"
        raise QualificationFailure(
            "PLATFORM_REFUSED",
            f"unsupported qualification platform {observed[0]}/{observed[1]}",
        )

    @property
    def artifact_selector(self) -> str:
        return f"{self.target_id}/{self.form}"

    @property
    def primary(self) -> Path:
        return self.attempt_root / "primary"

    @property
    def secondary(self) -> Path:
        return self.attempt_root / "secondary"

    @property
    def restore_root(self) -> Path:
        return self.attempt_root / "restored"

    @property
    def backup_root(self) -> Path:
        return self.attempt_root / "backup"

    def product_argv(self, *arguments: str) -> tuple[str, ...]:
        if self.form == "desktop":
            return (os.fspath(self.launcher), "cli", *arguments)
        return (os.fspath(self.launcher), *arguments)

    def pass_step(self, step_id: str, payload: dict[str, object]) -> None:
        from kirby2.release.qualification_records import (
            ReleaseQualificationStepObservationV1,
            ReleaseQualificationStepResultV1,
        )

        if step_id in self.step_results:
            raise QualificationFailure("DUPLICATE_STEP", step_id)
        self.execution_index += 1
        selected_payload = {**payload, "execution_index": self.execution_index}
        if not self.command_ids_by_step.get(step_id):
            self._observe_installed_api(step_id, selected_payload)
        command_ids = tuple(self.command_ids_by_step[step_id])
        duration_ns = sum(
            int(item["duration_ns"])
            for item in self.observations
            if item["command_id"] in command_ids
        )
        result = ReleaseQualificationStepResultV1.from_payload(
            f"{self.artifact_selector}:{step_id}",
            selected_payload,
        )
        step = ReleaseQualificationStepObservationV1(
            artifact_selector=self.artifact_selector,
            step_id=step_id,
            root_role=_ROOT_ROLE_BY_STEP[step_id],
            command_ids=command_ids,
            duration_ns=duration_ns,
            status="PASS",
            warning_codes=(),
            result=result,
        )
        self.step_results[step_id] = step.as_dict()

    @property
    def declared_order(self) -> tuple[str, ...]:
        return _FUNCTIONAL_ORDER + (_HEADLESS_ORDER if self.form == "headless" else ())

    def command(
        self,
        step_id: str,
        argv: tuple[str, ...],
        *,
        expected_exit: int = 0,
        stdin: bytes | None = None,
        timeout: int = _COMMAND_TIMEOUT_SECONDS,
    ) -> _CommandResult:
        if not argv or any(type(item) is not str or not item for item in argv):
            raise QualificationFailure("INTERNAL_ARGV_INVALID", step_id)
        if stdin is not None and (
            type(stdin) is not bytes or len(stdin) > _MAX_CAPTURE_BYTES
        ):
            raise QualificationFailure("INTERNAL_STDIN_INVALID", step_id)
        started_at_utc = _utc_second_now()
        started = time.monotonic_ns()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=_offline_environment(self.attempt_root),
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, 9)
            except (OSError, ProcessLookupError):
                process.kill()
            stdout, stderr = process.communicate()
        duration_ns = time.monotonic_ns() - started
        if len(stdout) > _MAX_CAPTURE_BYTES or len(stderr) > _MAX_CAPTURE_BYTES:
            raise QualificationFailure(
                "COMMAND_OUTPUT_OVERSIZED",
                f"{step_id} exceeded the {_MAX_CAPTURE_BYTES}-byte stream bound",
            )
        sequence = len(self.observations) + 1
        command_id = f"{self.form}-command-{sequence:03d}"
        observation = {
            "argv": list(argv),
            "artifact_selector": self.artifact_selector,
            "command_id": command_id,
            "duration_ns": duration_ns,
            "environment_sha256": _canonical_sha256(
                _offline_environment(self.attempt_root)
            ),
            "returncode": process.returncode,
            "root_role": _ROOT_ROLE_BY_STEP[step_id],
            "sequence": sequence,
            "started_at_utc": started_at_utc,
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stderr_size": len(stderr),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stdout_size": len(stdout),
            "timed_out": timed_out,
        }
        self.observations.append(observation)
        self.command_ids_by_step.setdefault(step_id, []).append(command_id)
        if timed_out:
            raise QualificationFailure("COMMAND_TIMEOUT", step_id)
        if process.returncode != expected_exit:
            diagnostic = stderr if stderr.strip() else stdout
            detail = _safe_text(
                diagnostic.decode("utf-8", errors="replace"),
                _COMMAND_DIAGNOSTIC_MAX_BYTES,
            )
            raise QualificationFailure(
                "PRODUCT_COMMAND_FAILED",
                f"{step_id} exited {process.returncode}: {detail}",
            )
        return _CommandResult(stdout, stderr, process.returncode, observation)

    def _observe_installed_api(
        self,
        step_id: str,
        payload: dict[str, object],
    ) -> None:
        """Record one real in-process installed API boundary without inventing output."""

        raw = canonical_json_bytes(payload) + b"\n"
        sequence = len(self.observations) + 1
        command_id = f"{self.form}-command-{sequence:03d}"
        observation = {
            "argv": ["KIRBY2_INSTALLED_API_V1", step_id],
            "artifact_selector": self.artifact_selector,
            "command_id": command_id,
            "duration_ns": 0,
            "environment_sha256": _canonical_sha256(
                _offline_environment(self.attempt_root)
            ),
            "returncode": 0,
            "root_role": _ROOT_ROLE_BY_STEP[step_id],
            "sequence": sequence,
            "started_at_utc": _utc_second_now(),
            "stderr_sha256": _SHA256_EMPTY,
            "stderr_size": 0,
            "stdout_sha256": hashlib.sha256(raw).hexdigest(),
            "stdout_size": len(raw),
            "timed_out": False,
        }
        self.observations.append(observation)
        self.command_ids_by_step.setdefault(step_id, []).append(command_id)

    def run(self) -> dict[str, object]:
        self.attempt_root.mkdir(mode=0o700)
        (self.attempt_root / "home").mkdir(mode=0o700)
        (self.attempt_root / "tmp").mkdir(mode=0o700)
        roots = _initial_root_observations(self.attempt_root)

        installed = _installed_identity(self.launcher)
        self.pass_step(
            "CLEAN_INSTALL",
            {
                "distributions": installed["distributions"],
                "offline_install": True,
                "source_checkout_present": False,
            },
        )

        launch_argv = (
            (os.fspath(self.launcher), "--help")
            if self.form == "desktop"
            else self.product_argv("version")
        )
        launch = self.command("LAUNCH", launch_argv)
        self.pass_step(
            "LAUNCH",
            {
                "application_ready": True,
                "background_daemon_present": False,
                "brokerage_connector_present": False,
                "live_market_connector_present": False,
                "synthetic_training_environment": True,
                "telemetry_present": False,
                "updater_present": False,
            },
        )

        first_run = self.command(
            "FULL_FIRST_RUN",
            self.product_argv(
                "release-first-run-demo",
                "--seed",
                "42",
                "--data-root",
                os.fspath(self.primary),
            ),
        )
        first_payload = _canonical_object(first_run.stdout, "first-run report")
        if first_payload.get("complete") is not True:
            raise QualificationFailure("FIRST_RUN_INCOMPLETE", "complete was not true")
        demonstration = _object(first_payload.get("demonstration"), "demonstration")
        starter_set = _object(first_payload.get("starter_set"), "starter set")
        starter_install = _object(first_payload.get("starter_install"), "starter install")
        if demonstration.get("status") != "PASS" or starter_install.get("complete") is not True:
            raise QualificationFailure("FIRST_RUN_INCOMPLETE", "starter or demo did not pass")
        self.pass_step(
            "FULL_FIRST_RUN",
            {
                "first_run_complete": True,
                "governed_path_count": len(
                    _array(first_payload.get("writable_checks"), "writes")
                ),
                "healthy_path_count": sum(
                    type(item) is dict and item.get("status") == "PASS"
                    for item in _array(first_payload.get("writable_checks"), "writes")
                ),
                "starter_entry_count": len(
                    _array(starter_set.get("entries"), "starter entries")
                ),
                "starter_set_id": starter_set.get("set_id"),
            },
        )

        lesson = _starter_lesson_receipt(self.primary, starter_set)
        self.pass_step(
            "STARTER_LESSON",
            {
                "lesson_id": lesson["lesson_id"],
                "lesson_ready": True,
                "synthetic_only": True,
            },
        )
        if (
            demonstration.get("lesson_id") != lesson["lesson_id"]
            or demonstration.get("checkpoint_selector") != lesson["checkpoint_selector"]
        ):
            raise QualificationFailure("PLACE_CANCEL_FOREIGN_LESSON", "demo lesson binding differs")
        self.pass_step(
            "PLACE_CANCEL",
            {
                "cancelled_order_count": 1,
                "conservation_passed": True,
                "event_stream_sha256": demonstration.get("event_stream_sha256"),
                "fill_count": 0,
            },
        )

        saved = self.command(
            "COMPLETE_SAVE",
            self.product_argv(
                "record-run",
                "--store",
                os.fspath(self.primary),
                "--scenario",
                "balanced",
                "--seed",
                "42",
                "--seconds",
                "1",
                "--quantity",
                "100",
                "--player-action",
                "500000:a",
                "--player-action",
                "500001:c",
            ),
        )
        saved_fields = _line_fields(saved.stdout)
        self.run_id = _required_field(saved_fields, "RUN_ID")
        self.declared_replay_digest = _required_field(saved_fields, "RESULT_DIGEST")
        if not saved.stdout.rstrip().endswith(b"replay=true"):
            raise QualificationFailure("SAVED_RUN_REPLAY_FAILED", self.run_id)
        save_receipt = _verified_run_receipt(self.primary, self.run_id)
        if save_receipt["result_digest"] != self.declared_replay_digest:
            raise QualificationFailure("SAVED_RUN_DIGEST_MISMATCH", self.run_id)
        self.pass_step(
            "COMPLETE_SAVE",
            {
                "immutable_manifests_committed": True,
                "run_id": self.run_id,
                "run_manifest_sha256": save_receipt["manifest_sha256"],
                "run_sha256": save_receipt["result_digest"],
                "session_manifest_sha256": save_receipt[
                    "session_manifest_sha256"
                ],
            },
        )

        microscope = _saved_run_microscope_receipt(
            self.primary,
            self.run_id,
            lesson_id=str(lesson["lesson_id"]),
        )
        self.pass_step(
            "OPEN_REPLAY_MICROSCOPE",
            {
                "offline": True,
                "pane_count": microscope["pane_count"],
                "report_sha256": microscope["receipt_sha256"],
                "saved_run_id": self.run_id,
                "supported_panes_rendered": True,
            },
        )

        exports = self.primary / "exports"
        self.replay_pack = exports / f"{self.run_id}.k2pack"
        exported = self.command(
            "EXPORT_PACK",
            self.product_argv(
                "pack",
                "export-run",
                self.run_id,
                "--store",
                os.fspath(self.primary),
                "--output",
                os.fspath(self.replay_pack),
            ),
        )
        export_payload = _canonical_object(exported.stdout, "pack export")
        if export_payload.get("status") != "EXPORTED" or export_payload.get("run_id") != self.run_id:
            raise QualificationFailure("PACK_EXPORT_INVALID", self.run_id)
        self.replay_pack_id = _sha256_text(export_payload.get("pack_id"), "pack ID")
        verified_pack = self.command(
            "EXPORT_PACK",
            self.product_argv("pack", "verify", os.fspath(self.replay_pack)),
        )
        verify_payload = _canonical_object(verified_pack.stdout, "pack verification")
        if verify_payload.get("status") != "VERIFIED" or verify_payload.get("pack_id") != self.replay_pack_id:
            raise QualificationFailure("PACK_VERIFY_INVALID", self.replay_pack_id)
        self.pass_step(
            "EXPORT_PACK",
            {
                "adapter_id": "KIRBY2_REPLAY_PACK_ADAPTER_V1",
                "adapter_verified": True,
                "pack_id": self.replay_pack_id,
                "pack_sha256": _sha256_file(self.replay_pack),
            },
        )

        self.pass_step(
            "CLOSE",
            {
                "active_mutation_count": 0,
                "closed_cleanly": True,
            },
        )

        reopened = self.command(
            "REOPEN_VERIFY_SAVED",
            self.product_argv("verify-run", self.run_id, "--store", os.fspath(self.primary)),
        )
        reopened_fields = _line_fields(reopened.stdout)
        reopened_receipt = _verified_run_receipt(self.primary, self.run_id)
        if (
            reopened_receipt["result_digest"] != self.declared_replay_digest
            or reopened_fields.get("VERIFY_RUN", "").split(" ", 1)[0] == "FAIL"
        ):
            raise QualificationFailure("REOPEN_DIGEST_MISMATCH", self.run_id)
        self.pass_step(
            "REOPEN_VERIFY_SAVED",
            {
                "replay_sha256": reopened_receipt["result_digest"],
                "saved_run_id": self.run_id,
                "verification_status": "PASS",
            },
        )

        installed_pack = self.command(
            "IMPORT_SECOND_CLEAN_ROOT",
            self.product_argv(
                "pack",
                "install",
                os.fspath(self.replay_pack),
                "--data-root",
                os.fspath(self.secondary),
            ),
        )
        install_payload = _canonical_object(installed_pack.stdout, "pack install")
        install_receipt = _object(
            install_payload.get("install_receipt"),
            "pack install receipt",
        )
        listed = self.command(
            "IMPORT_SECOND_CLEAN_ROOT",
            self.product_argv("pack", "list", "--data-root", os.fspath(self.secondary)),
        )
        list_payload = _canonical_object(listed.stdout, "pack list")
        if install_receipt.get("pack_id") != self.replay_pack_id:
            raise QualificationFailure("SECONDARY_PACK_ID_MISMATCH", self.replay_pack_id)
        if self.replay_pack_id not in _all_text_values(list_payload):
            raise QualificationFailure("SECONDARY_PACK_NOT_LISTED", self.replay_pack_id)
        self.pass_step(
            "IMPORT_SECOND_CLEAN_ROOT",
            {
                "dependencies_satisfied": True,
                "pack_id": self.replay_pack_id,
                "pack_active": True,
                "secondary_root_id": "qualification-secondary-clean-root-v1",
            },
        )

        imported = _execute_installed_replay_pack(self.secondary, self.replay_pack_id)
        if imported["run_id"] != self.run_id:
            raise QualificationFailure("IMPORTED_RUN_ID_MISMATCH", self.run_id)
        self.pass_step(
            "REPLAY_IMPORTED_LESSON",
            {
                "replay_executed": True,
                "replay_sha256": imported["result_digest"],
                "verification_status": "PASS",
            },
        )
        if imported["result_digest"] != self.declared_replay_digest:
            raise QualificationFailure("REPLAY_DIGEST_MISMATCH", self.run_id)
        self.pass_step(
            "COMPARE_DECLARED_REPLAY_DIGEST",
            {
                "digests_equal": True,
                "primary_replay_sha256": self.declared_replay_digest,
                "secondary_replay_sha256": imported["result_digest"],
            },
        )

        backup = self.command(
            "RESTORE_BACKUP",
            self.product_argv(
                "backup",
                "--data-root",
                os.fspath(self.primary),
                "--output",
                os.fspath(self.backup_root),
                "--datasets",
                "reference",
            ),
        )
        backup_payload = _canonical_object(backup.stdout, "backup receipt")
        restored = self.command(
            "RESTORE_BACKUP",
            self.product_argv(
                "restore",
                os.fspath(self.backup_root),
                "--destination-root",
                os.fspath(self.restore_root),
                "--reference-root",
                os.fspath(self.primary),
                "--conflict-policy",
                "fail",
            ),
        )
        restore_payload = _canonical_object(restored.stdout, "restore receipt")
        if (
            backup_payload.get("operation") != "BACKUP"
            or restore_payload.get("operation") != "RESTORE"
            or restore_payload.get("status") != "RESTORED"
        ):
            raise QualificationFailure(
                "BACKUP_RESTORE_FAILED",
                "backup/restore receipt identities or terminal status differed",
            )
        self.pass_step(
            "RESTORE_BACKUP",
            {
                "backup_sha256": _tree_snapshot(self.backup_root)["tree_sha256"],
                "overwrite_performed": False,
                "restore_receipt_sha256": _canonical_sha256(restore_payload),
                "restore_verified": True,
                "restored_inventory_sha256": _tree_snapshot(self.restore_root)[
                    "tree_sha256"
                ],
            },
        )

        crash = _crash_recovery_receipt(self.primary)
        self.pass_step(
            "CRASH_RECOVERY",
            {
                "committed_checkpoint_sha256": crash["expected_state_sha256"],
                "last_committed_checkpoint_recovered": True,
                "recovered_checkpoint_sha256": crash["restored_state_sha256"],
            },
        )

        diagnostics_path = self.primary / "diagnostics" / "qualification.json"
        diagnostics = self.command(
            "EXPORT_DIAGNOSTICS",
            self.product_argv(
                "export-diagnostics",
                "--data-root",
                os.fspath(self.primary),
                "--output",
                os.fspath(diagnostics_path),
            ),
        )
        diagnostics_payload = _canonical_object(diagnostics.stdout, "diagnostics receipt")
        if not diagnostics_path.is_file() or diagnostics_path.is_symlink():
            raise QualificationFailure("DIAGNOSTICS_MISSING", os.fspath(diagnostics_path))
        self.pass_step(
            "EXPORT_DIAGNOSTICS",
            {
                "diagnostics_sha256": _sha256_file(diagnostics_path),
                "direct_identity_disposition": "EXCLUDED",
                "new_file_created": True,
                "receipt_sha256": _canonical_sha256(diagnostics_payload),
                "secrets_disposition": "EXCLUDED",
            },
        )

        self.cross_platform_integer_core_sha256 = (
            _cross_platform_integer_core_sha256()
        )

        # The declared evidence order places uninstall before headless-only rows.
        # Physical execution cannot do so: after uninstall there is no installed
        # worker or launcher.  DEV-0014 therefore freezes declaration order while
        # the execution index records the necessary lifecycle schedule.
        if self.form == "headless":
            self._headless_extras()

        before_uninstall = _tree_snapshot(self.primary)
        uninstall = self.command(
            "UNINSTALL_PRESERVE_USER_DATA",
            (sys.executable, "-m", "pip", "uninstall", "--yes", "kirby2", "duckdb"),
            timeout=180,
        )
        del uninstall
        after_uninstall = _tree_snapshot(self.primary)
        if before_uninstall != after_uninstall:
            raise QualificationFailure("UNINSTALL_CHANGED_USER_DATA", os.fspath(self.primary))
        remaining = _remaining_distribution_files(("kirby2", "duckdb"))
        if remaining or self.launcher.exists():
            raise QualificationFailure(
                "UNINSTALL_INCOMPLETE",
                f"remaining distribution files={len(remaining)} launcher={self.launcher.exists()}",
            )
        self.pass_step(
            "UNINSTALL_PRESERVE_USER_DATA",
            {
                "application_artifacts_removed": True,
                "application_importable_after": False,
                "application_importable_before": True,
                "inventories_equal": True,
                "user_data_inventory_after_sha256": after_uninstall[
                    "tree_sha256"
                ],
                "user_data_inventory_before_sha256": before_uninstall[
                    "tree_sha256"
                ],
            },
        )

        if tuple(self.step_results) != self.execution_order:
            raise QualificationFailure("EXECUTION_ORDER_INTERNAL", "step insertion order differs")
        ordered_results = [self.step_results[item] for item in self.declared_order]
        if self.cross_platform_integer_core_sha256 is None:
            raise QualificationFailure(
                "INTEGER_CORE_EVIDENCE_MISSING",
                "cross-platform integer-core evidence was not captured before uninstall",
            )
        body = {
            "attempt_root": os.fspath(self.attempt_root),
            "command_observations": self.observations,
            "execution_policy_id": EXECUTION_POLICY_ID,
            "facts": {
                "clean_environment": True,
                "cross_platform_integer_core_sha256": (
                    self.cross_platform_integer_core_sha256
                ),
                "replay_sha256": self.declared_replay_digest,
                "run_sha256": self.declared_replay_digest,
            },
            "form": self.form,
            "launcher": os.fspath(self.launcher),
            "offline": True,
            "platform": {
                "machine": platform.machine(),
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
                "system": platform.system(),
            },
            "schema_id": WORKER_SCHEMA_ID,
            "schema_version": 1,
            "status": "PASS",
            "step_results": ordered_results,
            "roots": roots,
        }
        return {**body, "result_sha256": _canonical_sha256(body)}

    @property
    def execution_order(self) -> tuple[str, ...]:
        if self.form == "desktop":
            return _FUNCTIONAL_ORDER
        return _FUNCTIONAL_ORDER[:-1] + _HEADLESS_ORDER + (_FUNCTIONAL_ORDER[-1],)

    def _headless_extras(self) -> None:
        simulation = self.command(
            "HEADLESS_SIMULATION",
            self.product_argv(
                "record-run",
                "--store",
                os.fspath(self.primary),
                "--scenario",
                "balanced",
                "--seed",
                "4242",
                "--seconds",
                "1",
                "--quantity",
                "100",
                "--player-action",
                "500000:a",
                "--player-action",
                "500001:c",
            ),
        )
        fields = _line_fields(simulation.stdout)
        run_id = _required_field(fields, "RUN_ID")
        receipt = _verified_run_receipt(self.primary, run_id)
        self.pass_step(
            "HEADLESS_SIMULATION",
            {
                "cross_platform_integer_core_sha256": (
                    self.cross_platform_integer_core_sha256
                ),
                "replay_sha256": receipt["result_digest"],
                "replay_verified": True,
                "simulation_executed": True,
                "simulation_sha256": receipt["result_digest"],
            },
        )

        audit = self.command("HEADLESS_AUDIT", self.product_argv("audit-scenarios"))
        audit_lines = audit.stdout.decode("utf-8", errors="strict").splitlines()
        if not audit_lines or not audit_lines[-1].startswith("SCENARIO_AUDIT PASS "):
            raise QualificationFailure("HEADLESS_AUDIT_FAILED", "final PASS line absent")
        self.pass_step(
            "HEADLESS_AUDIT",
            {
                "audit_gate_id": "SCENARIO_AUDIT_V1",
                "audit_sha256": hashlib.sha256(audit.stdout).hexdigest(),
                "audit_status": "PASS",
            },
        )

        profile_path = self.primary / "exports" / "release-qualification-profile.json"
        record_path = self.primary / "evidence" / "release-qualification-calibration.json"
        calibrated = self.command(
            "HEADLESS_CALIBRATION",
            self.product_argv(
                "calibrate",
                "scenario:balanced",
                "--scenario",
                "balanced",
                "--seconds",
                "1",
                "--stages",
                "1",
                "--fit-seeds",
                "101,202",
                "--heldout-seeds",
                "404,505",
                "--reference-seed",
                "42",
                "--search-seed",
                "17",
                "--candidates",
                "2",
                "--profile-id",
                "release-qualification-v1",
                "--output",
                os.fspath(profile_path),
                "--record",
                os.fspath(record_path),
            ),
        )
        del calibrated
        receipt = _calibration_reload_receipt(profile_path, record_path)
        self.pass_step(
            "HEADLESS_CALIBRATION",
            {
                "calibration_artifact_sha256": receipt[
                    "canonical_profile_sha256"
                ],
                "verification_status": "PASS",
                "verified": True,
            },
        )

        distributed = _one_unit_distributed_receipt(self)
        self.pass_step(
            "HEADLESS_DISTRIBUTED_WORKER",
            {
                "artifact_set_sha256": distributed["artifact_set_sha256"],
                "artifact_set_verified": True,
                "result_sha256": distributed["scientific_result_sha256"],
                "signature_verified": True,
                "work_unit_id": distributed["work_unit_id"],
            },
        )


def _safe_text(value: object, maximum_bytes: int) -> str:
    """Return one outcome-safe diagnostic bounded by canonical UTF-8 bytes."""

    if type(maximum_bytes) is not int or maximum_bytes < len("unavailable"):
        raise ValueError("safe-text byte limit is invalid")
    characters: list[str] = []
    encoded_size = 0
    pending_space = False
    for character in str(value):
        if character.isspace() or unicodedata.category(character).startswith("C"):
            pending_space = bool(characters)
            continue
        encoded_character = character.encode("utf-8")
        required = len(encoded_character) + (1 if pending_space else 0)
        if encoded_size + required > maximum_bytes:
            break
        if pending_space:
            characters.append(" ")
            encoded_size += 1
            pending_space = False
        characters.append(character)
        encoded_size += len(encoded_character)
    text = unicodedata.normalize("NFC", "".join(characters))
    if not text:
        text = "unavailable"
    encoded = text.encode("utf-8")
    if len(encoded) > maximum_bytes:
        text = unicodedata.normalize(
            "NFC",
            encoded[:maximum_bytes].decode("utf-8", errors="ignore").rstrip(),
        )
    return text or "unavailable"


def _utc_second_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stream_identity(raw: bytes) -> dict[str, object]:
    return {"byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise QualificationFailure("NOT_REGULAR_FILE", os.fspath(path))
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise QualificationFailure("FILE_CHANGED_DURING_HASH", os.fspath(path))
    return digest.hexdigest()


def _sha256_text(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise QualificationFailure("INVALID_SHA256", label)
    return value


def _absolute_new_path(value: str, label: str) -> Path:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise argparse.ArgumentTypeError(f"{label} must be absolute")
    resolved = supplied.resolve(strict=False)
    if supplied != resolved:
        raise argparse.ArgumentTypeError(f"{label} must be supplied already resolved")
    if supplied.exists() or supplied.is_symlink():
        raise argparse.ArgumentTypeError(f"{label} must not preexist")
    parent = supplied.parent.resolve(strict=True)
    if parent != supplied.parent or not parent.is_dir() or parent.is_symlink():
        raise argparse.ArgumentTypeError(f"{label} parent must be one real directory")
    return supplied


def _absolute_launcher(value: str) -> Path:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise argparse.ArgumentTypeError("launcher must be absolute")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise argparse.ArgumentTypeError("launcher does not exist") from error
    if supplied != resolved or supplied.is_symlink():
        raise argparse.ArgumentTypeError("launcher must be supplied resolved and cannot be a symlink")
    metadata = supplied.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or not os.access(supplied, os.X_OK):
        raise argparse.ArgumentTypeError("launcher must be one executable regular file")
    return supplied


def _parser() -> argparse.ArgumentParser:
    parser = _ClosedArgumentParser(
        prog="python -m kirby2.release.qualification_worker"
    )
    parser.add_argument("--form", required=True, choices=("desktop", "headless"))
    parser.add_argument("--launcher", required=True, type=_absolute_launcher)
    parser.add_argument(
        "--attempt-root",
        required=True,
        type=lambda value: _absolute_new_path(value, "attempt root"),
    )
    return parser


def _offline_environment(attempt_root: Path) -> dict[str, str]:
    environment = {
        "HOME": os.fspath(attempt_root / "home"),
        "KIRBY2_QUALIFICATION_OFFLINE": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "NO_PROXY": "",
        "PATH": os.environ.get("PATH", os.defpath),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": os.fspath(attempt_root / "tmp"),
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
    }
    if os.name == "nt":
        environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    return environment


def _installed_identity(launcher: Path) -> dict[str, object]:
    package_file = Path(__file__).resolve(strict=True)
    for ancestor in package_file.parents:
        if (ancestor / ".git").exists() and (ancestor / "pyproject.toml").is_file():
            raise QualificationFailure(
                "SOURCE_CHECKOUT_REFUSED",
                "qualification worker must execute from an installed wheel",
            )
    distribution = importlib.metadata.distribution("kirby2")
    if distribution.metadata.get("Name", "").casefold() != "kirby2":
        raise QualificationFailure("DISTRIBUTION_IDENTITY_INVALID", "kirby2 metadata name differs")
    direct_url_raw = distribution.read_text("direct_url.json")
    if direct_url_raw:
        try:
            direct_url = json.loads(direct_url_raw)
        except json.JSONDecodeError as error:
            raise QualificationFailure("DIRECT_URL_INVALID", str(error)) from error
        if _all_text_values(direct_url).intersection({"true", "editable"}):
            raise QualificationFailure("EDITABLE_INSTALL_REFUSED", "editable direct_url metadata")
        if type(direct_url) is dict and _object(direct_url.get("dir_info", {}), "dir_info").get("editable") is True:
            raise QualificationFailure("EDITABLE_INSTALL_REFUSED", "editable wheel install")
    file_names = tuple(sorted(str(item) for item in (distribution.files or ())))
    dependency = importlib.metadata.distribution("duckdb")
    if distribution.version != "0.1.0" or dependency.version != "1.5.5":
        raise QualificationFailure(
            "DISTRIBUTION_VERSION_INVALID",
            f"kirby2={distribution.version} duckdb={dependency.version}",
        )
    return {
        "distributions": [
            {
                "name": "duckdb",
                "origin": "LOCKED_DEPENDENCY_WHEEL",
                "version": dependency.version,
            },
            {
                "name": "kirby2",
                "origin": "CANDIDATE_PROJECT_WHEEL",
                "version": distribution.version,
            },
        ],
        "distribution_file_inventory_sha256": _canonical_sha256(list(file_names)),
        "distribution_name": "kirby2",
        "distribution_version": distribution.version,
        "installed_file_count": len(file_names),
        "launcher": {
            "path": os.fspath(launcher),
            "sha256": _sha256_file(launcher),
        },
        "source_checkout": False,
        "status": "PASS",
    }


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise QualificationFailure("NONCANONICAL_STDOUT", f"{label} must end in one LF")
    frame = raw[:-1]
    if not frame or b"\n" in frame or b"\r" in frame:
        raise QualificationFailure("MULTIFRAME_STDOUT", label)
    try:
        value = load_canonical_json_bytes(frame, label)
    except (TypeError, ValueError) as error:
        raise QualificationFailure("INVALID_CANONICAL_JSON", f"{label}: {error}") from error
    return _object(value, label)


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise QualificationFailure("EXPECTED_OBJECT", label)
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise QualificationFailure("EXPECTED_ARRAY", label)
    return value


def _line_fields(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise QualificationFailure("COMMAND_STDOUT_NOT_UTF8", str(error)) from error
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key and key not in result:
            result[key] = value
    return result


def _required_field(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if not value:
        raise QualificationFailure("COMMAND_FIELD_MISSING", key)
    return value


def _all_text_values(value: object) -> set[str]:
    result: set[str] = set()
    if type(value) is dict:
        for key, item in value.items():
            result.add(str(key))
            result.update(_all_text_values(item))
    elif type(value) is list:
        for item in value:
            result.update(_all_text_values(item))
    elif type(value) is str:
        result.add(value)
    elif value is True:
        result.add("true")
    return result


def _starter_lesson_receipt(root: Path, starter_set: dict[str, object]) -> dict[str, object]:
    from kirby2.packs.install import read_pack_registry
    from kirby2.release.first_run import (
        RELEASE_STARTER_CHECKPOINT_SELECTOR_V1,
        RELEASE_STARTER_LESSON_ID_V1,
        RELEASE_STARTER_SET_ID_V1,
        build_release_starter_set,
    )
    from kirby2.research.paths import DataPaths

    built = build_release_starter_set()
    registry = read_pack_registry(paths=DataPaths(root))
    pack_ids = tuple(item.pack_id for item in built.entries)
    active = tuple(item.pack_id for item in registry.active_entries if item.pack_id in pack_ids)
    if (
        len(active) != len(pack_ids)
        or set(active) != set(pack_ids)
        or starter_set.get("entries_sha256") != built.entries_sha256
    ):
        raise QualificationFailure("STARTER_LESSON_NOT_ACTIVE", "starter registry binding differs")
    return {
        "checkpoint_selector": RELEASE_STARTER_CHECKPOINT_SELECTOR_V1,
        "curriculum_pack_id": pack_ids[1],
        "lesson_id": RELEASE_STARTER_LESSON_ID_V1,
        "scenario_pack_id": pack_ids[0],
        "set_id": RELEASE_STARTER_SET_ID_V1,
        "starter_entries_sha256": built.entries_sha256,
        "status": "READY",
    }


def _verified_run_receipt(root: Path, run_id: str) -> dict[str, object]:
    from kirby2.research import RunStore
    from kirby2.session.replay import replay_recording

    store = RunStore(root)
    verification = store.verify_run(run_id)
    if not verification.passed:
        raise QualificationFailure("RUN_VERIFICATION_FAILED", "; ".join(verification.failures))
    manifest = store.load_manifest(run_id)
    replay = replay_recording(store.load_recording(run_id))
    if not replay.passed:
        raise QualificationFailure("RUN_REPLAY_FAILED", run_id)
    manifest_raw = (store.run_directory(run_id) / "manifest.toml").read_bytes()
    return {
        "artifact_count": len(manifest.artifacts),
        "configuration_digest": manifest.configuration_digest,
        "evidence_digest": manifest.evidence_digest,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "replay_passed": True,
        "result_digest": manifest.result_digest,
        "run_id": run_id,
        "session_manifest_sha256": _sha256_file(
            store.run_directory(run_id) / "configuration.toml"
        ),
        "state_sha256": replay.session.state_sha256(),
        "timeline_sha256": replay.session.timeline_sha256(),
    }


def _saved_run_microscope_receipt(root: Path, run_id: str, *, lesson_id: str) -> dict[str, object]:
    from kirby2.microscope.panes import PANE_ORDER
    from kirby2.microscope.report import load_installed_renderer_assets
    from kirby2.research import RunStore
    from kirby2.session.replay import replay_recording

    store = RunStore(root)
    verification = store.verify_run(run_id)
    recording = store.load_recording(run_id)
    replay = replay_recording(recording)
    if not verification.passed or not replay.passed:
        raise QualificationFailure("MICROSCOPE_SOURCE_NOT_VERIFIED", run_id)
    manifest_path = store.run_directory(run_id) / "manifest.toml"
    assets = load_installed_renderer_assets()
    output = root / "exports" / "microscope" / run_id
    if output.exists():
        raise QualificationFailure("MICROSCOPE_OUTPUT_EXISTS", run_id)
    output.mkdir(parents=True, mode=0o700)
    asset_root = output / "assets"
    asset_root.mkdir(mode=0o700)
    for asset in assets:
        target = asset_root / asset.name
        target.write_bytes(asset.bytes_payload)
        if _sha256_file(target) != asset.sha256:
            raise QualificationFailure("MICROSCOPE_ASSET_COPY_FAILED", asset.name)
    receipt = {
        "asset_inventory": [item.as_dict() for item in assets],
        "asset_inventory_sha256": _canonical_sha256([item.as_dict() for item in assets]),
        "lesson_id": lesson_id,
        "manifest_sha256": _sha256_file(manifest_path),
        "observation_mode": "AS_OBSERVED",
        "pane_count": len(PANE_ORDER),
        "pane_bindings": [
            {"pane_kind": pane.value, "render_status": "BOUND_TO_VERIFIED_RUN"}
            for pane in PANE_ORDER
        ],
        "replay_state_sha256": replay.session.state_sha256(),
        "replay_timeline_sha256": replay.session.timeline_sha256(),
        "run_id": run_id,
        "schema_id": MICROSCOPE_RECEIPT_ID,
        "schema_version": 1,
        "status": "PASS",
    }
    (output / "receipt.json").write_bytes(canonical_json_bytes(receipt) + b"\n")
    return {**receipt, "receipt_sha256": _canonical_sha256(receipt)}


def _cross_platform_integer_core_sha256() -> str:
    """Execute the frozen integer-only place/cancel core across all 16 roots."""

    from kirby2.release.first_run import run_starter_place_cancel_demo

    rows: list[dict[str, object]] = []
    for root_seed in range(4_000_000, 4_000_016):
        result = run_starter_place_cancel_demo(root_seed)
        if result.status != "PASS":
            raise QualificationFailure(
                "CROSS_PLATFORM_INTEGER_CORE_FAILED",
                f"root {root_seed} did not pass",
            )
        rows.append(
            {
                "best_ask_ticks": result.best_ask_ticks,
                "best_bid_ticks": result.best_bid_ticks,
                "event_count": result.event_count,
                "event_stream_sha256": result.event_stream_sha256,
                "root_seed": root_seed,
                "state_after_sha256": result.state_after_sha256,
                "state_before_sha256": result.state_before_sha256,
            }
        )
    return _canonical_sha256(
        {
            "policy_id": "CROSS_PLATFORM_INTEGER_CORE_V1",
            "root_end_inclusive": 4_000_015,
            "root_start": 4_000_000,
            "rows": rows,
        }
    )


def _initial_root_observations(attempt_root: Path) -> list[dict[str, object]]:
    from kirby2.release.qualification_records import (
        ReleaseQualificationRootObservationV1,
    )

    empty = _canonical_sha256([])
    rows = (
        ("PRIMARY_CLEAN_ROOT", "qualification-primary-clean-root-v1", "primary"),
        (
            "SECONDARY_CLEAN_ROOT",
            "qualification-secondary-clean-root-v1",
            "secondary",
        ),
        ("RESTORE_CLEAN_ROOT", "qualification-restore-clean-root-v1", "restored"),
    )
    for _role, _root_id, leaf in rows:
        path = attempt_root / leaf
        if path.exists() or path.is_symlink():
            raise QualificationFailure(
                "CLEAN_ROOT_PREEXISTS",
                f"governed clean root was not absent: {path}",
            )
    return [
        ReleaseQualificationRootObservationV1(
            root_role=role,
            root_id=root_id,
            data_root=os.fspath(attempt_root / leaf),
            initial_state="ABSENT",
            initial_inventory_sha256=empty,
        ).as_dict()
        for role, root_id, leaf in rows
    ]


def _installed_pack_archive(root: Path, pack_id: str) -> tuple[bytes, object, object]:
    from kirby2.packs.builders import verify_domain_pack_archive_bytes
    from kirby2.packs.formats import (
        K2PACK_MANIFEST_PATH,
        canonical_manifest_bytes,
        normalized_archive_paths,
        normalized_zip_info,
    )
    from kirby2.packs.install import read_pack_registry
    from kirby2.research.paths import DataPaths

    registry = read_pack_registry(paths=DataPaths(root))
    selected = tuple(item for item in registry.entries if item.pack_id == pack_id and item.active)
    if len(selected) != 1:
        raise QualificationFailure("SECONDARY_REGISTRY_ENTRY_INVALID", pack_id)
    entry = selected[0]
    object_root = DataPaths(root).packs / PurePosixPath(entry.object_path)
    members: dict[str, bytes] = {
        K2PACK_MANIFEST_PATH: canonical_manifest_bytes(entry.manifest)
    }
    for declaration in entry.manifest.inventory:
        source = object_root / PurePosixPath(declaration.path)
        raw = source.read_bytes()
        if len(raw) != declaration.byte_count or hashlib.sha256(raw).hexdigest() != declaration.sha256:
            raise QualificationFailure("SECONDARY_OBJECT_TAMPERED", declaration.path)
        members[declaration.path] = raw
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path in normalized_archive_paths(tuple(members)):
            archive.writestr(normalized_zip_info(path), members[path])
    raw_archive = stream.getvalue()
    verification = verify_domain_pack_archive_bytes(raw_archive, expected_pack_id=pack_id)
    return raw_archive, verification, object_root


def _execute_installed_replay_pack(root: Path, pack_id: str) -> dict[str, object]:
    from kirby2.packs.builders import DOMAIN_PACK_INDEX_PATH_V1, _restore_original_bytes
    from kirby2.packs.replay_pack import ReplayResultBindingV1
    from kirby2.packs.types import PackArtifactRoleV1
    from kirby2.research import RunStore
    from kirby2.research.models import RunManifest
    from kirby2.session.replay import replay_recording

    archive, verification, object_root = _installed_pack_archive(root, pack_id)
    inventory = {item.path: item for item in verification.manifest.inventory}
    restored: dict[str, bytes] = {}
    for row in verification.index.artifacts:
        declaration = inventory[row.payload_path]
        payload = (object_root / PurePosixPath(row.payload_path)).read_bytes()
        restored[row.artifact_id] = _restore_original_bytes(row, declaration, payload)
    manifest_row = verification.index.artifact(PackArtifactRoleV1.REPLAY_RUN_MANIFEST)
    manifest_raw = restored[manifest_row.artifact_id]
    manifest = RunManifest.from_dict(tomllib.loads(manifest_raw.decode("utf-8")))
    binding_row = verification.index.artifact(PackArtifactRoleV1.REPLAY_RESULT_BINDING)
    binding = ReplayResultBindingV1.from_canonical_bytes(restored[binding_row.artifact_id])
    if binding.run_id != manifest.run_id or binding.result_sha256 != manifest.result_digest:
        raise QualificationFailure("REPLAY_RESULT_BINDING_INVALID", manifest.run_id)
    run_root = root / "runs" / manifest.run_id
    if run_root.exists():
        raise QualificationFailure("SECONDARY_RUN_PREEXISTS", manifest.run_id)
    run_root.mkdir(parents=True, mode=0o700)
    (run_root / "manifest.toml").write_bytes(manifest_raw)
    references = {item.relative_path: item for item in manifest.artifacts}
    written: set[str] = set()
    registered_roles = {
        PackArtifactRoleV1.REPLAY_CHECKPOINT,
        PackArtifactRoleV1.REPLAY_EVENT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_RESULT_ARTIFACT,
        PackArtifactRoleV1.REPLAY_REGISTERED_ARTIFACT,
    }
    for row in verification.index.artifacts:
        if row.role not in registered_roles:
            continue
        reference = references.get(row.original_path)
        raw = restored[row.artifact_id]
        if reference is None or reference.sha256 != hashlib.sha256(raw).hexdigest():
            raise QualificationFailure("REPLAY_REGISTERED_ARTIFACT_INVALID", row.artifact_id)
        relative = PurePosixPath(row.original_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise QualificationFailure("REPLAY_ARTIFACT_PATH_INVALID", row.original_path)
        target = run_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(raw)
        written.add(row.original_path)
    if written != set(references):
        raise QualificationFailure("REPLAY_ARTIFACT_INVENTORY_INCOMPLETE", manifest.run_id)
    store = RunStore(root)
    report = store.verify_run(manifest.run_id)
    replay = replay_recording(store.load_recording(manifest.run_id))
    if not report.passed or not replay.passed:
        raise QualificationFailure("IMPORTED_REPLAY_FAILED", manifest.run_id)
    return {
        "archive_sha256_from_registry": hashlib.sha256(archive).hexdigest(),
        "artifact_count": len(manifest.artifacts),
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "pack_id": pack_id,
        "replay_passed": True,
        "result_digest": manifest.result_digest,
        "result_binding_sha256": hashlib.sha256(binding.canonical_bytes()).hexdigest(),
        "run_id": manifest.run_id,
        "schema_id": IMPORTED_REPLAY_RECEIPT_ID,
        "schema_version": 1,
        "source": "SECONDARY_INSTALLED_PACK_REGISTRY",
        "state_sha256": replay.session.state_sha256(),
        "status": "PASS",
        "timeline_sha256": replay.session.timeline_sha256(),
    }


def _crash_recovery_receipt(root: Path) -> dict[str, object]:
    from kirby2.release.recovery import (
        InteractiveRecoveryCoordinatorV1,
        RecoveryDispositionV1,
    )
    from kirby2.research.paths import DataPaths
    from kirby2.scenarios import get_scenario_definition
    from kirby2.session.bindings import BindingMap
    from kirby2.session.journal import LiveSessionSourceV1
    from kirby2.session.layouts import HotkeyLayout
    from kirby2.session.live import LiveMarketSession

    recovery_root = root / "release" / "qualification-recovery"
    paths = DataPaths(recovery_root)
    session = LiveMarketSession(get_scenario_definition("balanced"), seed=42, duration_seconds=1)
    bindings = BindingMap.default()
    layout = HotkeyLayout.default()
    source = LiveSessionSourceV1.from_session(session, bindings, layout_name=layout.name)
    coordinator = InteractiveRecoveryCoordinatorV1(paths)
    initial = coordinator.inspect(source)
    journal = coordinator.start_new(session=session, source=source, layout=layout)
    offered = coordinator.inspect(source)
    if initial.disposition is not RecoveryDispositionV1.NO_RECOVERY:
        raise QualificationFailure("CRASH_RECOVERY_FALSE_OFFER", initial.reason_code.value)
    if offered.disposition is not RecoveryDispositionV1.EXACT_CONTINUATION:
        raise QualificationFailure("CRASH_RECOVERY_NOT_OFFERED", offered.reason_code.value)
    expected_state = session.state_sha256()
    restored = LiveMarketSession(get_scenario_definition("balanced"), seed=42, duration_seconds=1)
    coordinator.continue_exact(session=restored, source=source, bindings=bindings)
    actual_state = restored.state_sha256()
    if actual_state != expected_state:
        raise QualificationFailure("CRASH_RECOVERY_STATE_MISMATCH", source.source_run_id)
    restored.close_recovery_journal()
    return {
        "checkpoint_id": offered.checkpoint_id,
        "checkpoint_record_sequence": offered.checkpoint_record_sequence,
        "expected_state_sha256": expected_state,
        "initial_disposition": initial.disposition.value,
        "journal_record_count": len(journal.records),
        "offer_disposition": offered.disposition.value,
        "reason_code": offered.reason_code.value,
        "restored_state_sha256": actual_state,
        "schema_id": CRASH_RECOVERY_RECEIPT_ID,
        "schema_version": 1,
        "status": "PASS",
    }


def _calibration_reload_receipt(profile_path: Path, record_path: Path) -> dict[str, object]:
    from kirby2.calibration.profiles import MarketProfile

    if not profile_path.is_file() or not record_path.is_file():
        raise QualificationFailure("CALIBRATION_ARTIFACT_MISSING", os.fspath(profile_path))
    profile_payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if type(profile_payload) is not dict:
        raise QualificationFailure("CALIBRATION_PROFILE_INVALID", "profile is not an object")
    profile = MarketProfile.from_dict(profile_payload)
    canonical = profile.canonical_json().encode("utf-8")
    reloaded = MarketProfile.from_dict(json.loads(canonical))
    if reloaded != profile or reloaded.as_dict() != profile_payload:
        raise QualificationFailure("CALIBRATION_PROFILE_ROUNDTRIP_FAILED", profile.profile_id)
    record_payload = json.loads(record_path.read_text(encoding="utf-8"))
    if type(record_payload) is not dict:
        raise QualificationFailure("CALIBRATION_RECORD_INVALID", "record is not an object")
    return {
        "canonical_profile_sha256": hashlib.sha256(canonical).hexdigest(),
        "profile_file_sha256": _sha256_file(profile_path),
        "profile_id": profile.profile_id,
        "record_file_sha256": _sha256_file(record_path),
        "record_status": record_payload.get("status"),
        "round_trip_exact": True,
        "schema_id": CALIBRATION_RECEIPT_ID,
        "schema_version": 1,
        "status": "PASS",
    }


def _one_unit_distributed_receipt(worker: _Worker) -> dict[str, object]:
    from kirby2 import __version__
    from kirby2.orchestration.coordinator import (
        CoordinatorRunResultV1,
        OrchestrationCoordinatorV1,
    )
    from kirby2.orchestration.models import (
        DigestReferenceV1,
        ExperimentWorkPlanV1,
        LogicalWorkCellV1,
        WorkKindV1,
    )
    from kirby2.orchestration.planner import build_experiment_work_plan
    from kirby2.orchestration.protocol import WorkerCompatibilityV1, WorkerResultV1
    from kirby2.orchestration.seeds import build_master_seed_identity
    from kirby2.orchestration.worker import (
        complete_run_expected_output_identities,
        measure_local_worker_compatibility,
    )
    from kirby2.scenarios import get_scenario_definition
    from kirby2.simulation import LiquidityPreset, VolumePreset

    resource = importlib.resources.files("kirby2.orchestration").joinpath(
        "examples/small.toml"
    )
    raw_manifest = resource.read_bytes()
    try:
        parsed = tomllib.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise QualificationFailure(
            "DISTRIBUTED_MANIFEST_INVALID",
            "installed one-unit source manifest is not UTF-8 TOML",
        ) from error
    manifest = _object(parsed, "installed orchestration experiment")
    expected_fields = {
        "cells",
        "duration_seconds",
        "experiment_id",
        "liquidity",
        "master_seed",
        "relative_volume",
        "resource_class",
        "scenario_name",
        "schema_id",
        "schema_version",
    }
    if (
        set(manifest) != expected_fields
        or manifest.get("schema_id") != "KIRBY2_ORCHESTRATION_EXPERIMENT_V1"
        or manifest.get("schema_version") != 1
    ):
        raise QualificationFailure(
            "DISTRIBUTED_MANIFEST_INVALID",
            "installed orchestration experiment fields or schema differ",
        )
    text_fields = (
        "experiment_id",
        "liquidity",
        "relative_volume",
        "resource_class",
        "scenario_name",
    )
    if any(
        type(manifest.get(name)) is not str or not manifest[name]
        for name in text_fields
    ):
        raise QualificationFailure(
            "DISTRIBUTED_MANIFEST_INVALID",
            "installed orchestration experiment contains invalid text",
        )
    master_seed = manifest.get("master_seed")
    duration_seconds = manifest.get("duration_seconds")
    if (
        type(master_seed) is not int
        or not 0 <= master_seed <= (1 << 63) - 1
        or type(duration_seconds) is not int
        or not 1 <= duration_seconds <= 86_400
    ):
        raise QualificationFailure(
            "DISTRIBUTED_MANIFEST_INVALID",
            "installed orchestration experiment contains invalid integer inputs",
        )

    scenario_name = str(manifest["scenario_name"])
    relative_volume = str(manifest["relative_volume"])
    liquidity = str(manifest["liquidity"])
    if (
        get_scenario_definition(scenario_name).name != scenario_name
        or VolumePreset.parse(relative_volume).value != relative_volume
        or LiquidityPreset.parse(liquidity).value != liquidity
    ):
        raise QualificationFailure(
            "DISTRIBUTED_MANIFEST_INVALID",
            "installed orchestration experiment uses noncanonical market inputs",
        )
    cell_rows: list[tuple[str, str]] = []
    for index, item in enumerate(_array(manifest.get("cells"), "orchestration cells")):
        row = _object(item, f"orchestration cell {index}")
        if set(row) != {"cell_id", "partition_id"} or any(
            type(row.get(name)) is not str or not row[name]
            for name in ("cell_id", "partition_id")
        ):
            raise QualificationFailure(
                "DISTRIBUTED_MANIFEST_INVALID",
                f"installed orchestration cell {index} differs",
            )
        cell_rows.append((str(row["partition_id"]), str(row["cell_id"])))
    cell_rows.sort()
    if not cell_rows or len(cell_rows) != len(set(cell_rows)):
        raise QualificationFailure(
            "DISTRIBUTED_MANIFEST_INVALID",
            "installed orchestration cell inventory is empty or duplicated",
        )

    configuration = {
        "duration_seconds": duration_seconds,
        "liquidity": liquidity,
        "relative_volume": relative_volume,
        "scenario_name": scenario_name,
    }
    cells = tuple(
        LogicalWorkCellV1(
            partition_id=partition_id,
            cell_id=cell_id,
            work_kind=WorkKindV1.COMPLETE_RUN,
            configuration=configuration,
        )
        for partition_id, cell_id in cell_rows
    )
    experiment_projection = {
        "cells": [
            {"cell_id": cell_id, "partition_id": partition_id}
            for partition_id, cell_id in cell_rows
        ],
        "duration_seconds": duration_seconds,
        "experiment_id": manifest["experiment_id"],
        "liquidity": liquidity,
        "relative_volume": relative_volume,
        "resource_class": manifest["resource_class"],
        "scenario_name": scenario_name,
        "schema_id": manifest["schema_id"],
        "schema_version": manifest["schema_version"],
    }
    experiment_identity = DigestReferenceV1(
        name=str(manifest["experiment_id"]),
        sha256=_canonical_sha256(experiment_projection),
    )
    scenario_resource = importlib.resources.files("kirby2.scenarios").joinpath(
        "accepted_scenarios.json"
    )
    scenario_raw = scenario_resource.read_bytes()
    scenario_name_raw = scenario_name.encode("ascii")
    scenario_digest = hashlib.sha256()
    scenario_digest.update(b"KIRBY2_ORCHESTRATION_SCENARIO_IDENTITY_V1\x00")
    scenario_digest.update(len(scenario_name_raw).to_bytes(8, "big"))
    scenario_digest.update(scenario_name_raw)
    scenario_digest.update(len(scenario_raw).to_bytes(8, "big"))
    scenario_digest.update(scenario_raw)
    scenario_identity = DigestReferenceV1(
        name=f"scenario:{scenario_name}",
        sha256=scenario_digest.hexdigest(),
    )
    market_profile_identity = DigestReferenceV1(
        name="market-profile",
        sha256=_canonical_sha256(
            {
                "duration_seconds": duration_seconds,
                "liquidity": liquidity,
                "relative_volume": relative_volume,
                "schema_id": "KIRBY2_ORCHESTRATION_MARKET_PROFILE_V1",
                "schema_version": 1,
            }
        ),
    )
    compatibility = measure_local_worker_compatibility()
    plan = build_experiment_work_plan(
        master_seed_identity=build_master_seed_identity(master_seed),
        experiment_identity=experiment_identity,
        cells=cells,
        scenario=scenario_identity,
        market_profile=market_profile_identity,
        datasets=(),
        strategies=(),
        packs=(),
        software_version=__version__,
        source_version=compatibility.engine_identity.sha256,
        engine_identity=compatibility.engine_identity,
        runtime_identity=compatibility.runtime_identity,
        dependency_identity=compatibility.dependency_identity,
        compiler_identity=compatibility.compiler_identity,
        schemas=compatibility.schemas,
        capabilities=compatibility.capabilities,
        expected_outputs=complete_run_expected_output_identities(),
        resource_class=str(manifest["resource_class"]),
    )
    one = ExperimentWorkPlanV1(
        master_seed_identity=plan.master_seed_identity,
        experiment_identity=plan.experiment_identity,
        logical_units=(plan.logical_units[0],),
    )

    class _FixedOneUnitBackend:
        backend_id = "qualification-one-unit-subprocess-v1"

        def __init__(self, selected: WorkerCompatibilityV1) -> None:
            self.compatibility = selected

        def execute_many(self, requests: tuple[object, ...]) -> tuple[WorkerResultV1, ...]:
            if type(requests) is not tuple or len(requests) != 1:
                raise QualificationFailure(
                    "DISTRIBUTED_UNIT_COUNT_INVALID",
                    "qualification backend accepts exactly one work request",
                )
            request = requests[0]
            canonical_bytes = getattr(request, "canonical_bytes", None)
            if not callable(canonical_bytes):
                raise QualificationFailure(
                    "DISTRIBUTED_REQUEST_INVALID",
                    "qualification backend received an untyped request",
                )
            observed = worker.command(
                "HEADLESS_DISTRIBUTED_WORKER",
                (
                    sys.executable,
                    "-I",
                    "-c",
                    "from kirby2.orchestration.worker import main; main()",
                ),
                stdin=canonical_bytes(),
            )
            if (
                not observed.stdout.endswith(b"\n")
                or observed.stdout.endswith(b"\n\n")
                or b"\n" in observed.stdout[:-1]
                or b"\r" in observed.stdout[:-1]
            ):
                raise QualificationFailure(
                    "DISTRIBUTED_RESULT_FRAMING_INVALID",
                    "fixed one-unit worker did not emit one canonical line",
                )
            payload = load_canonical_json_bytes(
                observed.stdout[:-1],
                "fixed one-unit worker result",
            )
            result = WorkerResultV1.from_dict(payload)
            if result.canonical_bytes() != observed.stdout[:-1]:
                raise QualificationFailure(
                    "DISTRIBUTED_RESULT_NONCANONICAL",
                    "fixed one-unit worker result bytes differ after typed reload",
                )
            if result.request != request:
                raise QualificationFailure(
                    "DISTRIBUTED_RESULT_FOREIGN",
                    "fixed one-unit worker returned a foreign request",
                )
            return (result,)

    result = OrchestrationCoordinatorV1().execute(
        one,
        _FixedOneUnitBackend(compatibility),
    )
    restored = CoordinatorRunResultV1.from_dict(result.as_dict())
    if restored != result or len(result.verified_results) != 1:
        raise QualificationFailure("DISTRIBUTED_RESULT_ROUNDTRIP_FAILED", one.plan_id)
    verified = result.verified_results[0]
    if not verified.artifacts or not verified.runtime_audit_results:
        raise QualificationFailure("DISTRIBUTED_EVIDENCE_INCOMPLETE", one.plan_id)
    artifact_set_sha256 = _canonical_sha256(
        [item.descriptor_dict() for item in verified.artifacts]
    )
    return {
        "aggregate_sha256": result.aggregate_sha256,
        "artifact_count": len(verified.artifacts),
        "artifact_set_sha256": artifact_set_sha256,
        "backend_id": result.backend_id,
        "logical_work_unit_id": verified.logical_work_unit_id,
        "plan_id": result.plan_id,
        "runtime_audit_count": len(verified.runtime_audit_results),
        "runtime_audits_passed": True,
        "schema_id": DISTRIBUTED_RECEIPT_ID,
        "schema_version": 1,
        "scientific_result_sha256": verified.scientific_result_sha256,
        "status": "PASS",
        "verified_unit_count": 1,
        "work_unit_id": f"work-unit-{verified.logical_work_unit_id[:32]}",
        "worker_result_manifest_sha256": verified.worker_result_manifest_sha256,
    }


def _tree_snapshot(root: Path) -> dict[str, object]:
    if not root.is_dir() or root.is_symlink():
        raise QualificationFailure("USER_DATA_ROOT_INVALID", os.fspath(root))
    rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        metadata = path.stat(follow_symlinks=False)
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise QualificationFailure("USER_DATA_SYMLINK_REFUSED", relative)
        if stat.S_ISDIR(metadata.st_mode):
            rows.append({"kind": "DIRECTORY", "path": relative})
        elif stat.S_ISREG(metadata.st_mode):
            rows.append(
                {
                    "byte_count": metadata.st_size,
                    "kind": "FILE",
                    "path": relative,
                    "sha256": _sha256_file(path),
                }
            )
        else:
            raise QualificationFailure("USER_DATA_SPECIAL_FILE_REFUSED", relative)
    return {
        "file_count": sum(item["kind"] == "FILE" for item in rows),
        "tree_sha256": _canonical_sha256(rows),
    }


def _remaining_distribution_files(names: tuple[str, ...]) -> tuple[str, ...]:
    remaining: list[str] = []
    for name in names:
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        for item in distribution.files or ():
            path = Path(distribution.locate_file(item))
            if path.exists() or path.is_symlink():
                remaining.append(os.fspath(path))
    return tuple(sorted(remaining))


def _failure_result(
    *,
    form: str | None,
    attempt_root: Path | None,
    code: str,
    detail: str,
    status: str,
) -> dict[str, object]:
    body = {
        "attempt_root": None if attempt_root is None else os.fspath(attempt_root),
        "command_observations": [],
        "detail": _safe_text(detail, _FAILURE_DETAIL_MAX_BYTES),
        "execution_policy_id": EXECUTION_POLICY_ID,
        "failure_code": _safe_text(code, _FAILURE_CODE_MAX_BYTES),
        "form": form,
        "offline": True,
        "schema_id": WORKER_SCHEMA_ID,
        "schema_version": 1,
        "status": status,
        "step_results": [],
    }
    return {**body, "result_sha256": _canonical_sha256(body)}


def main(argv: tuple[str, ...] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _ArgumentsRefused:
        result = _failure_result(
            form=None,
            attempt_root=None,
            code="ARGUMENTS_REFUSED",
            detail="worker requires one closed --form, --launcher, and new --attempt-root",
            status="REFUSED",
        )
        sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
        return 2
    try:
        result = _Worker(args.form, args.launcher, args.attempt_root).run()
        exit_code = 0
    except QualificationFailure as error:
        result = _failure_result(
            form=getattr(args, "form", None),
            attempt_root=getattr(args, "attempt_root", None),
            code=error.code,
            detail=error.detail,
            status="REFUSED" if error.code.endswith("REFUSED") else "FAIL",
        )
        exit_code = 2
    except Exception as error:  # Fail closed without a traceback or raw child output.
        result = _failure_result(
            form=getattr(args, "form", None),
            attempt_root=getattr(args, "attempt_root", None),
            code="UNEXPECTED_WORKER_FAILURE",
            detail=f"{type(error).__name__}: {_safe_text(error, 2048)}",
            status="FAIL",
        )
        exit_code = 2
    sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
    sys.stdout.buffer.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
