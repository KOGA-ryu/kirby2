"""Registered offline release data, first-run, and diagnostic commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.packs.formats import canonical_json_bytes
from kirby2.research.paths import DataAreaId, DataPaths

from .backup import (
    BackupFamilyV1,
    BackupSelectionV1,
    DatasetBackupPolicyV1,
    create_backup,
    verify_backup,
)
from .restore import RestoreConflictPolicyV1, restore_backup
from .diagnostics import export_release_diagnostics
from .doctor import HealthStatusV1, release_identity, run_doctor, verify_installation
from .first_run import run_first_run
from .platform_paths import select_release_paths
from .build import (
    ReleaseCommandStatusV1,
    build_release_artifacts,
    load_release_protocol_bundle,
    release_resource_preflight,
    verify_release_artifacts,
)
from .performance import (
    RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1,
    RunnerSourceTreeV1,
    bind_performance_row_template,
    build_performance_row_template,
)
from .qualification import (
    ReleaseEvidenceReferenceV1,
    verify_closeout_prerequisites,
)
from .qualification_executor import execute_release_qualification


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be explicit and absolute")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise argparse.ArgumentTypeError("path cannot be resolved safely") from error
    if path != resolved:
        raise argparse.ArgumentTypeError("path must be supplied already resolved")
    return resolved


def _family_name(item: BackupFamilyV1) -> str:
    return item.value.casefold().replace("_", "-")


_FAMILY_BY_NAME = {_family_name(item): item for item in BackupFamilyV1}


def _configure_backup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", required=True, type=_absolute_path)
    parser.add_argument("--output", required=True, type=_absolute_path)
    parser.add_argument(
        "--family",
        action="append",
        choices=tuple(_FAMILY_BY_NAME),
        help="selected family; repeat to override the all-portable default",
    )
    parser.add_argument(
        "--datasets",
        choices=("embed", "reference", "omit"),
        default="reference",
    )
    parser.add_argument("--consent", default="LOCAL_USER_REQUEST_V1")
    parser.add_argument(
        "--redaction-policy",
        default="EXCLUDE_DIRECT_IDENTITY_V1",
    )
    parser.add_argument(
        "--max-embedded-dataset-bytes",
        type=int,
        default=512 * 1024 * 1024,
    )


def _handle_backup(args: argparse.Namespace) -> int:
    if args.family is not None and len(args.family) != len(set(args.family)):
        raise ValueError("backup families must not be repeated")
    families = (
        tuple(BackupFamilyV1)
        if args.family is None
        else tuple(item for item in BackupFamilyV1 if _family_name(item) in args.family)
    )
    selection = BackupSelectionV1(
        families=families,
        dataset_policy=DatasetBackupPolicyV1(args.datasets.upper()),
        consent_id=args.consent,
        redaction_policy_id=args.redaction_policy,
        max_embedded_dataset_bytes=args.max_embedded_dataset_bytes,
    )
    result = create_backup(
        paths=DataPaths(args.data_root),
        selection=selection,
        destination=args.output,
    )
    _print_json({"operation": "BACKUP", **result.as_dict()})
    return 0


def _configure_restore(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("backup_root", type=_absolute_path)
    parser.add_argument("--destination-root", required=True, type=_absolute_path)
    parser.add_argument("--reference-root", type=_absolute_path)
    parser.add_argument(
        "--conflict-policy",
        choices=("fail", "accept-identical-only"),
        default="fail",
    )
    parser.add_argument("--consent", default="LOCAL_USER_REQUEST_V1")


def _handle_restore(args: argparse.Namespace) -> int:
    policy = RestoreConflictPolicyV1(
        args.conflict_policy.upper().replace("-", "_")
    )
    result = restore_backup(
        backup_root=args.backup_root,
        destination_paths=DataPaths(args.destination_root),
        reference_paths=(
            None if args.reference_root is None else DataPaths(args.reference_root)
        ),
        conflict_policy=policy,
        accepted_consent_id=args.consent,
    )
    _print_json({"operation": "RESTORE", **result.as_dict()})
    return 0


def _configure_backup_restore_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--seed", required=True, type=int)


def _handle_backup_restore_demo(args: argparse.Namespace) -> int:
    if args.fixture != "release-user-data":
        raise ValueError("backup/restore demo fixture must be release-user-data")
    with tempfile.TemporaryDirectory(prefix="kirby2-backup-restore-") as temporary:
        root = Path(temporary).resolve()
        source_paths = DataPaths(root / "source")
        source_paths.ensure(
            (DataAreaId.CONFIG, DataAreaId.EVIDENCE, DataAreaId.DATASETS)
        )
        configuration = canonical_json_bytes(
            {
                "fixture": args.fixture,
                "profile_id": "profile-demo-pseudonymous",
                "seed": args.seed,
            }
        )
        evidence = canonical_json_bytes(
            {"annotation": "local deterministic backup fixture", "seed": args.seed}
        )
        dataset = canonical_json_bytes(
            {"dataset": "digest-referenced-demo", "seed": args.seed}
        )
        (source_paths.config / "profiles").mkdir()
        (source_paths.config / "profiles" / "demo.json").write_bytes(configuration)
        (source_paths.evidence / "annotations").mkdir()
        (source_paths.evidence / "annotations" / "demo.json").write_bytes(evidence)
        (source_paths.datasets / "demo.json").write_bytes(dataset)
        backup_root = root / "backup"
        backup = create_backup(
            paths=source_paths,
            selection=BackupSelectionV1.all_portable(
                dataset_policy=DatasetBackupPolicyV1.REFERENCE
            ),
            destination=backup_root,
        )
        destination_paths = DataPaths(root / "restored")
        restored = restore_backup(
            backup_root=backup_root,
            destination_paths=destination_paths,
            reference_paths=source_paths,
        )
        verified = verify_backup(backup_root)
        _print_json(
            {
                "backup": backup.as_dict(),
                "fixture": args.fixture,
                "manifest_verified": verified.manifest == backup.manifest,
                "restore": restored.as_dict(),
                "seed": args.seed,
                "status": "PASS",
            }
        )
    return 0


def _configure_data_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-root",
        type=_absolute_path,
        help="explicit resolved data root; platform release path is the default",
    )


def _paths_from_args(args: argparse.Namespace) -> DataPaths:
    return select_release_paths(explicit_root=args.data_root).paths


def _handle_version(_args: argparse.Namespace) -> int:
    _print_json(release_identity().as_dict())
    return 0


def _handle_data_paths(args: argparse.Namespace) -> int:
    selection = select_release_paths(explicit_root=args.data_root)
    _print_json(selection.as_dict())
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(_paths_from_args(args), strict=False)
    _print_json(report.as_dict())
    return 1 if report.status is HealthStatusV1.FAIL else 0


def _handle_verify_installation(args: argparse.Namespace) -> int:
    report = verify_installation(_paths_from_args(args))
    _print_json(report.as_dict())
    return 0 if report.status is HealthStatusV1.PASS else 1


def _configure_export_diagnostics(parser: argparse.ArgumentParser) -> None:
    _configure_data_root(parser)
    parser.add_argument("--output", required=True, type=_absolute_path)
    parser.add_argument(
        "--authorize-hidden-lesson-truth",
        action="store_true",
        help=(
            "record explicit authorization; V1 support diagnostics still do not "
            "collect hidden lesson truth"
        ),
    )


def _handle_export_diagnostics(args: argparse.Namespace) -> int:
    receipt = export_release_diagnostics(
        _paths_from_args(args),
        args.output,
        authorize_hidden_lesson_truth=args.authorize_hidden_lesson_truth,
    )
    _print_json(receipt.as_dict())
    return 0


def _configure_first_run_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument(
        "--data-root",
        type=_absolute_path,
        help=(
            "explicit persistent data root; when omitted the complete flow runs "
            "inside a clean temporary root"
        ),
    )


def _handle_first_run_demo(args: argparse.Namespace) -> int:
    if args.data_root is not None:
        report = run_first_run(DataPaths(args.data_root), seed=args.seed)
        _print_json(report.as_dict())
        return 0 if report.complete else 1
    with tempfile.TemporaryDirectory(prefix="kirby2-first-run-") as temporary:
        root = Path(temporary).resolve()
        report = run_first_run(DataPaths(root), seed=args.seed)
        _print_json(report.as_dict())
        return 0 if report.complete else 1


def _print_json(value: object) -> None:
    print(canonical_json_bytes(value).decode("ascii"))


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolved_path(value: Path) -> Path:
    return Path(os.path.abspath(os.fspath(value))).resolve(strict=False)


def _release_protocol_bundle():
    return load_release_protocol_bundle(_repository_root())


def _candidate_commit(value: str) -> str:
    if value == "HEAD":
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=_repository_root(),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("candidate HEAD cannot be resolved to a commit")
        try:
            value = result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ValueError("candidate HEAD is not an ASCII commit ID") from error
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("candidate must be a forty-character lowercase commit ID")
    return value


def _require_protocol_path(value: Path, relative: str) -> Path:
    supplied = _resolved_path(value)
    expected = _repository_root() / relative
    if supplied != expected:
        raise ValueError(f"protocol path must be the frozen {relative}")
    return supplied


def _not_exercised(command_id: str, detail: str, missing: list[str]) -> int:
    bundle = _release_protocol_bundle()
    _print_json(
        {
            "command_id": command_id,
            "detail": detail,
            "missing": missing,
            "protocol_set_sha256": bundle.protocol_set_sha256,
            "schema_id": "KIRBY2_RELEASE_COMMAND_OUTCOME_V1",
            "schema_version": 1,
            "status": ReleaseCommandStatusV1.NOT_EXERCISED.value,
        }
    )
    return 2


def _configure_build_release(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--artifact-store", required=True, type=Path)


def _handle_build_release(args: argparse.Namespace) -> int:
    _require_protocol_path(args.protocol, "release/qualification.toml")
    bundle = _release_protocol_bundle()
    outcome = build_release_artifacts(
        bundle,
        candidate_commit=_candidate_commit(args.candidate),
        artifact_root=_resolved_path(args.artifact_store),
    )
    _print_json(outcome.as_dict())
    return 0 if outcome.status is ReleaseCommandStatusV1.COMPLETE else 2


def _configure_verify_release_artifacts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--artifact-store", required=True, type=Path)


def _handle_verify_release_artifacts(args: argparse.Namespace) -> int:
    bundle = _release_protocol_bundle()
    candidate = _candidate_commit(args.candidate)
    outcome = verify_release_artifacts(
        bundle,
        _resolved_path(args.artifact_store),
        candidate_commit=candidate,
    )
    _print_json(outcome.as_dict())
    if outcome.status is ReleaseCommandStatusV1.NOT_EXERCISED:
        return 2
    return 0 if outcome.status is ReleaseCommandStatusV1.PASS else 1


def _configure_resource_preflight(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platforms", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path, default=Path("release/wheelhouse"))
    parser.add_argument(
        "--provider-inventory",
        type=Path,
        default=Path(".kirby2/release/clean-providers.toml"),
    )


def _handle_resource_preflight(args: argparse.Namespace) -> int:
    if not args.no_network:
        raise ValueError("release resource preflight requires --no-network")
    _require_protocol_path(args.platforms, "release/platforms.toml")
    _require_protocol_path(args.lock, "release/requirements.lock")
    _require_protocol_path(args.qualification, "release/qualification.toml")
    bundle = _release_protocol_bundle()
    report = release_resource_preflight(
        bundle,
        wheelhouse_root=_resolved_path(args.wheelhouse),
        provider_inventory=(
            None
            if args.provider_inventory is None
            else _resolved_path(args.provider_inventory)
        ),
    )
    output = _resolved_path(args.output)
    output.write_text(report.markdown(), encoding="utf-8")
    _print_json({**report.as_dict(), "output": os.fspath(output)})
    return 0 if report.status == "PASS" else 2


def _configure_qualify_release(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platform", required=True, choices=("macos-arm64", "linux-x86_64"))
    parser.add_argument("--build-evidence", required=True, type=Path)
    parser.add_argument("--artifact-store", required=True, type=Path)


def _handle_qualify_release(args: argparse.Namespace) -> int:
    bundle = _release_protocol_bundle()
    outcome = execute_release_qualification(
        bundle,
        target_id=args.platform,
        build_evidence=_resolved_path(args.build_evidence),
        artifact_root=_resolved_path(args.artifact_store),
    )
    _print_json(outcome.as_dict())
    if outcome.status in {
        ReleaseCommandStatusV1.PASS,
        ReleaseCommandStatusV1.PASS_WITH_WARNINGS,
    }:
        return 0
    if outcome.status is ReleaseCommandStatusV1.FAIL:
        return 1
    return 2


def _configure_qualify_performance(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--complete-run-work-units", required=True, type=int)
    parser.add_argument("--build-evidence", required=True, type=Path)
    parser.add_argument("--artifact-store", required=True, type=Path)


def _handle_qualify_performance(args: argparse.Namespace) -> int:
    manifest = _require_protocol_path(args.manifest, "release/performance_thresholds.toml")
    if args.complete_run_work_units != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1:
        raise ValueError("release performance requires exactly 10,000 complete work units")
    bundle = _release_protocol_bundle()
    build_evidence = _resolved_path(args.build_evidence)
    artifact_store = _resolved_path(args.artifact_store)
    source_lock = _repository_root() / "release/performance_runner_sources.lock"
    missing = [
        os.fspath(path)
        for path in (
            build_evidence,
            artifact_store / "release-artifact-index.json",
            source_lock,
        )
        if not path.is_file()
    ]
    if missing:
        return _not_exercised(
            "QUALIFY_PERFORMANCE",
            "Performance dispatch awaits the frozen candidate, artifact index, and runner-source lock.",
            missing,
        )
    _print_json(
        {
            "artifact_store": os.fspath(artifact_store),
            "build_evidence_sha256": hashlib.sha256(build_evidence.read_bytes()).hexdigest(),
            "command_id": "QUALIFY_PERFORMANCE",
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "protocol_set_sha256": bundle.protocol_set_sha256,
            "queue_size": bundle.performance_protocol.queue_size,
            "row_corpus_sha256": bundle.performance_protocol.row_corpus_sha256,
            "row_count": bundle.performance_protocol.row_count,
            "schema_version": 1,
            "status": "READY",
            "worker_count": bundle.performance_protocol.worker_count,
        }
    )
    return 0


def _configure_qualify_performance_row(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--work-unit-id", required=True)
    parser.add_argument("--attempt", required=True, type=int, choices=(1, 2))


def _handle_qualify_performance_row(args: argparse.Namespace) -> int:
    _require_protocol_path(args.protocol, "release/performance_thresholds.toml")
    parts = args.work_unit_id.split("/")
    if len(parts) != 3 or parts[0] != "release-perf":
        raise ValueError("performance work-unit ID is invalid")
    try:
        root_seed = int(parts[2])
    except ValueError as error:
        raise ValueError("performance work-unit root is invalid") from error
    template = build_performance_row_template(parts[1], root_seed)
    if template.work_unit_id != args.work_unit_id:
        raise ValueError("performance work-unit ID is noncanonical")
    source_lock_path = _repository_root() / "release/performance_runner_sources.lock"
    if not source_lock_path.is_file():
        return _not_exercised(
            "QUALIFY_PERFORMANCE_ROW",
            "Row binding awaits the mechanically frozen runner-source tree.",
            [os.fspath(source_lock_path)],
        )
    source_tree = RunnerSourceTreeV1.from_bytes(source_lock_path.read_bytes())
    _print_json(
        {
            "attempt": args.attempt,
            "bound_row": bind_performance_row_template(template, source_tree),
            "command_id": "QUALIFY_PERFORMANCE_ROW",
            "schema_version": 1,
            "status": "READY",
        }
    )
    return 0


_CLOSEOUT_MARKER_START = "<!-- KIRBY2_RELEASE_CLOSEOUT_V1\n"
_CLOSEOUT_MARKER_END = "\nKIRBY2_RELEASE_CLOSEOUT_V1 -->"


def _configure_close_release(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build-evidence", required=True, type=Path)
    parser.add_argument("--macos-evidence", required=True, type=Path)
    parser.add_argument("--linux-evidence", required=True, type=Path)
    parser.add_argument("--performance-evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)


def _closeout_evidence_rows(args: argparse.Namespace) -> list[dict[str, object]]:
    rows = []
    repository = _repository_root()
    for gate_id, value in (
        ("WO40-F", args.build_evidence),
        ("WO40-G", args.macos_evidence),
        ("WO40-H", args.linux_evidence),
        ("WO40-I", args.performance_evidence),
    ):
        path = _resolved_path(value)
        try:
            relative = path.relative_to(repository).as_posix()
        except ValueError as error:
            raise ValueError(
                "closeout evidence must be inside the release repository"
            ) from error
        raw = path.read_bytes()
        rows.append(
            {
                "gate_id": gate_id,
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        )
    return rows


def _handle_close_release(args: argparse.Namespace) -> int:
    evidence_paths = tuple(
        _resolved_path(value)
        for value in (
            args.build_evidence,
            args.macos_evidence,
            args.linux_evidence,
            args.performance_evidence,
        )
    )
    missing = [os.fspath(path) for path in evidence_paths if not path.is_file()]
    prerequisites_path = _repository_root() / ".kirby2/release/closeout-prerequisites.json"
    if not prerequisites_path.is_file():
        missing.append(os.fspath(prerequisites_path))
    if missing:
        return _not_exercised(
            "CLOSE_RELEASE",
            "Closeout awaits every immutable platform/performance artifact and the prerequisite aggregate.",
            missing,
        )
    raw_prerequisites = json.loads(prerequisites_path.read_bytes())
    reference_rows = raw_prerequisites.get("references") if type(raw_prerequisites) is dict else None
    if type(reference_rows) is not list:
        raise ValueError("closeout prerequisite reference array is missing")
    references = tuple(
        ReleaseEvidenceReferenceV1(
            gate_id=row["gate_id"],
            evidence_id=row["evidence_id"],
            size=row["size"],
            sha256=row["sha256"],
            status=row["status"],
        )
        for row in reference_rows
        if type(row) is dict
        and set(row) == {"gate_id", "evidence_id", "size", "sha256", "status"}
    )
    if len(references) != len(reference_rows):
        raise ValueError("closeout prerequisite reference fields differ")
    prerequisites = verify_closeout_prerequisites(references)
    if prerequisites["status"] != "PASS":
        _print_json({"command_id": "CLOSE_RELEASE", **prerequisites})
        return 2
    payload = {
        "evidence": _closeout_evidence_rows(args),
        "prerequisites": prerequisites,
        "schema_id": "KIRBY2_RELEASE_CLOSEOUT_V1",
        "schema_version": 1,
        "status": "PASS",
    }
    canonical = canonical_json_bytes(payload).decode("ascii")
    output = _resolved_path(args.output)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(
            "# Kirby2 Release Closeout\n\nStatus: `PASS`\n\n"
            "This packet closes the bounded local simulation and training release.\n\n"
            + _CLOSEOUT_MARKER_START
            + canonical
            + _CLOSEOUT_MARKER_END
            + "\n"
        )
    _print_json({"closeout_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), **payload})
    return 0


def _configure_verify_closeout(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("closeout", type=Path)


def _handle_verify_closeout(args: argparse.Namespace) -> int:
    path = _resolved_path(args.closeout)
    text = path.read_text("utf-8")
    start = text.find(_CLOSEOUT_MARKER_START)
    end = text.find(_CLOSEOUT_MARKER_END, start + len(_CLOSEOUT_MARKER_START))
    if start < 0 or end < 0:
        raise ValueError("release closeout canonical payload is missing")
    raw = text[start + len(_CLOSEOUT_MARKER_START) : end].encode("ascii")
    payload = json.loads(raw)
    if canonical_json_bytes(payload) != raw:
        raise ValueError("release closeout payload is not canonical JSON")
    if type(payload) is not dict or set(payload) != {
        "evidence", "prerequisites", "schema_id", "schema_version", "status"
    }:
        raise ValueError("release closeout fields differ")
    if (
        payload["schema_id"] != "KIRBY2_RELEASE_CLOSEOUT_V1"
        or payload["schema_version"] != 1
        or payload["status"] != "PASS"
    ):
        raise ValueError("release closeout identity differs")
    prerequisites = payload["prerequisites"]
    if type(prerequisites) is not dict or set(prerequisites) != {
        "evidence_projection_sha256",
        "extra_gates",
        "missing_gates",
        "prerequisite_id",
        "schema_version",
        "status",
    }:
        raise ValueError("release closeout prerequisite fields differ")
    if (
        prerequisites["prerequisite_id"] != "WO40_J_PREREQUISITES_V1"
        or prerequisites["schema_version"] != 1
        or prerequisites["status"] != "PASS"
        or prerequisites["extra_gates"] != []
        or prerequisites["missing_gates"] != []
        or type(prerequisites["evidence_projection_sha256"]) is not str
        or len(prerequisites["evidence_projection_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in prerequisites["evidence_projection_sha256"]
        )
    ):
        raise ValueError("release closeout prerequisites are not passing and immutable")
    evidence = payload["evidence"]
    if type(evidence) is not list or [
        row.get("gate_id") if type(row) is dict else None for row in evidence
    ] != ["WO40-F", "WO40-G", "WO40-H", "WO40-I"]:
        raise ValueError("release closeout evidence order differs")
    failures = []
    repository = _repository_root()
    for row in evidence:
        if type(row) is not dict or set(row) != {
            "gate_id", "path", "sha256", "size"
        }:
            raise ValueError("release closeout evidence fields differ")
        if (
            type(row["path"]) is not str
            or type(row["size"]) is not int
            or row["size"] <= 0
            or type(row["sha256"]) is not str
            or len(row["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in row["sha256"])
        ):
            raise ValueError("release closeout evidence types differ")
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("release closeout evidence path is unsafe")
        source = (repository / relative).resolve(strict=False)
        try:
            source.relative_to(repository)
        except ValueError as error:
            raise ValueError("release closeout evidence resolves outside the repository") from error
        if not source.is_file():
            failures.append({"gate_id": row["gate_id"], "code": "EVIDENCE_MISSING"})
            continue
        source_raw = source.read_bytes()
        if len(source_raw) != row["size"] or hashlib.sha256(source_raw).hexdigest() != row["sha256"]:
            failures.append({"gate_id": row["gate_id"], "code": "EVIDENCE_IDENTITY_MISMATCH"})
    status = "PASS" if not failures and payload["status"] == "PASS" else "FAIL"
    _print_json(
        {
            "closeout_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "failures": failures,
            "schema_id": "KIRBY2_RELEASE_CLOSEOUT_VERIFICATION_V1",
            "schema_version": 1,
            "status": status,
        }
    )
    return 0 if status == "PASS" else 1


RELEASE_DATA_COMMAND_MODULE = CommandModule(
    module_id="RELEASE_DATA_BACKUP_RESTORE",
    commands=(
        CommandSpec(
            command_id="BACKUP_USER_DATA",
            name="backup",
            help="create one explicit content-addressed Kirby2 user-data backup",
            handler=_handle_backup,
            configure=_configure_backup,
        ),
        CommandSpec(
            command_id="RESTORE_USER_DATA",
            name="restore",
            help="verify and atomically restore a backup into a separate data root",
            handler=_handle_restore,
            configure=_configure_restore,
        ),
        CommandSpec(
            command_id="BACKUP_RESTORE_DEMO",
            name="backup-restore-demo",
            help="exercise the deterministic release user-data backup flow",
            handler=_handle_backup_restore_demo,
            configure=_configure_backup_restore_demo,
        ),
        CommandSpec(
            command_id="RELEASE_DOCTOR",
            name="doctor",
            help="inspect paths, packs, schemas, manifests, dependencies, and recovery",
            handler=_handle_doctor,
            configure=_configure_data_root,
        ),
        CommandSpec(
            command_id="RELEASE_VERSION",
            name="version",
            help="show engine, source, runtime, and schema identity",
            handler=_handle_version,
        ),
        CommandSpec(
            command_id="RELEASE_DATA_PATHS",
            name="data-paths",
            help="show every governed release data path without creating it",
            handler=_handle_data_paths,
            configure=_configure_data_root,
        ),
        CommandSpec(
            command_id="VERIFY_RELEASE_INSTALLATION",
            name="verify-installation",
            help="strictly verify a complete first-run release installation",
            handler=_handle_verify_installation,
            configure=_configure_data_root,
        ),
        CommandSpec(
            command_id="EXPORT_RELEASE_DIAGNOSTICS",
            name="export-diagnostics",
            help="export one new explicitly redacted local diagnostic JSON file",
            handler=_handle_export_diagnostics,
            configure=_configure_export_diagnostics,
        ),
        CommandSpec(
            command_id="RELEASE_FIRST_RUN_DEMO",
            name="release-first-run-demo",
            help="run the complete offline first-run and place/cancel demonstration",
            handler=_handle_first_run_demo,
            configure=_configure_first_run_demo,
        ),
        CommandSpec(
            command_id="BUILD_RELEASE",
            name="build-release",
            help="plan or execute the frozen offline release artifact build",
            handler=_handle_build_release,
            configure=_configure_build_release,
        ),
        CommandSpec(
            command_id="VERIFY_RELEASE_ARTIFACTS",
            name="verify-release-artifacts",
            help="verify the exact six-row release artifact index and transports",
            handler=_handle_verify_release_artifacts,
            configure=_configure_verify_release_artifacts,
        ),
        CommandSpec(
            command_id="RELEASE_RESOURCE_PREFLIGHT",
            name="release-resource-preflight",
            help="inspect frozen build wheels, tools, starters, and clean providers without network access",
            handler=_handle_resource_preflight,
            configure=_configure_resource_preflight,
        ),
        CommandSpec(
            command_id="QUALIFY_RELEASE",
            name="qualify-release",
            help="execute and deeply verify the frozen clean-environment matrix",
            handler=_handle_qualify_release,
            configure=_configure_qualify_release,
        ),
        CommandSpec(
            command_id="QUALIFY_PERFORMANCE",
            name="qualify-performance",
            help="dispatch the frozen auxiliary and 10,000-complete-run performance protocol",
            handler=_handle_qualify_performance,
            configure=_configure_qualify_performance,
        ),
        CommandSpec(
            command_id="QUALIFY_PERFORMANCE_ROW",
            name="qualify-performance-row",
            help="bind one preregistered performance row to the frozen runner-source tree",
            handler=_handle_qualify_performance_row,
            configure=_configure_qualify_performance_row,
        ),
        CommandSpec(
            command_id="CLOSE_RELEASE",
            name="close-release",
            help="create the immutable closeout packet after every prior release gate passes",
            handler=_handle_close_release,
            configure=_configure_close_release,
        ),
        CommandSpec(
            command_id="VERIFY_RELEASE_CLOSEOUT",
            name="verify-release-closeout",
            help="verify closeout canonical bytes and every referenced evidence artifact",
            handler=_handle_verify_closeout,
            configure=_configure_verify_closeout,
        ),
    ),
)


__all__ = ["RELEASE_DATA_COMMAND_MODULE"]
