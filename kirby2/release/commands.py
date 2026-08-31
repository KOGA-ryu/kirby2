"""Registered release backup and restore commands."""

from __future__ import annotations

import argparse
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


def _print_json(value: object) -> None:
    print(canonical_json_bytes(value).decode("ascii"))


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
    ),
)


__all__ = ["RELEASE_DATA_COMMAND_MODULE"]
