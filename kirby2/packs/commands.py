"""Generic WO39 pack lifecycle commands over the secure local substrate."""

from __future__ import annotations

import argparse
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.research.paths import DataAreaId, DataPaths
from kirby2.research.store import DEFAULT_RESEARCH_STORE

from .archive import (
    PackArchivePreflightV1,
    preflight_pack_archive_bytes,
    read_pack_archive_bytes,
)
from .builders import (
    DomainPackBuildV1,
    build_domain_pack,
    build_pack_source_directory,
    build_registered_run_pack,
    runtime_environment_for_verified_pack_v1,
    verify_domain_pack_archive_bytes,
    write_new_pack_archive,
)
from .formats import canonical_json_bytes, require_sha256
from .install import (
    deactivate_pack,
    install_pack,
    read_pack_registry,
    remove_deactivated_pack,
)
from .scenario_pack import build_scenario_demo_inputs
from .signatures import (
    PackAuthenticityVerificationV1,
    qualification_report_for_verified_pack,
    read_pack_signature_bytes,
    verify_pack_signature,
)
from .staging import discard_pack_stage, stage_pack_archive_bytes
from .types import DomainPackRefusalCodeV1, DomainPackRefused


def _data_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("data root must be an explicit absolute path")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise argparse.ArgumentTypeError("data root cannot be resolved safely") from error
    if path != resolved:
        raise argparse.ArgumentTypeError("data root must be supplied already resolved")
    return resolved


def _pack_id(value: str) -> str:
    try:
        return require_sha256(value, "pack ID")
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _configure_pack(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="pack_action", required=True)

    build = actions.add_parser(
        "build",
        help="build one confined pack-source.toml directory",
    )
    build.add_argument("source_directory", type=Path)
    build.add_argument("--output", type=Path)

    export_run = actions.add_parser(
        "export-run",
        help="package only one verified run's manifest-registered artifacts",
    )
    export_run.add_argument("run_id")
    export_run.add_argument("--store", type=Path, default=DEFAULT_RESEARCH_STORE)
    export_run.add_argument("--output", type=Path)

    inspect = actions.add_parser(
        "inspect",
        help="inspect a fully preflighted manifest without installing",
    )
    inspect.add_argument("archive", type=Path)
    inspect.add_argument(
        "--signature",
        type=Path,
        help="report one detached signature claim without weakening preflight",
    )

    verify = actions.add_parser(
        "verify",
        help="preflight and run the exact owning domain adapter",
    )
    verify.add_argument("archive", type=Path)
    verify.add_argument(
        "--signature",
        type=Path,
        help="report one detached signature claim separately from pack validity",
    )

    install = actions.add_parser(
        "install",
        help="verify, stage, and atomically install one local pack",
    )
    install.add_argument("archive", type=Path)
    install.add_argument(
        "--signature",
        type=Path,
        help="retain detached authenticity status in the install result",
    )
    install.add_argument("--data-root", required=True, type=_data_root)

    list_parser = actions.add_parser(
        "list",
        help="list the exact local pack registry",
    )
    list_parser.add_argument("--data-root", required=True, type=_data_root)

    remove = actions.add_parser(
        "remove",
        help="deactivate and recoverably remove one exact logical pack ID",
    )
    remove.add_argument("pack_id", type=_pack_id)
    remove.add_argument("--data-root", required=True, type=_data_root)


def _handle_pack(args: argparse.Namespace) -> int:
    if args.pack_action == "build":
        build = build_pack_source_directory(args.source_directory)
        output = args.output or _default_output(build)
        target = write_new_pack_archive(build, output)
        _print_json(_build_result(build, target))
        return 0
    if args.pack_action == "export-run":
        build = build_registered_run_pack(args.store, args.run_id)
        output = args.output or _default_output(build)
        target = write_new_pack_archive(build, output)
        result = _build_result(build, target)
        result.update(
            {
                "run_id": args.run_id,
                "source_policy": "MANIFEST_REGISTERED_ARTIFACTS_ONLY",
                "status": "EXPORTED",
            }
        )
        _print_json(result)
        return 0
    if args.pack_action == "inspect":
        raw = read_pack_archive_bytes(args.archive)
        preflight = preflight_pack_archive_bytes(raw)
        authenticity = _authenticity(preflight, args.signature)
        _print_json(
            {
                "archive_byte_count": preflight.archive_byte_count,
                "authenticity": authenticity.as_dict(),
                "inventory_sha256": preflight.inventory_sha256,
                "manifest": preflight.manifest.as_dict(),
                "manifest_sha256": preflight.manifest_sha256,
                "transport_sha256": preflight.transport_sha256,
                "validation_policy_id": preflight.validation_policy_id,
            }
        )
        return 0
    if args.pack_action == "verify":
        raw = read_pack_archive_bytes(args.archive)
        verification = verify_domain_pack_archive_bytes(raw)
        authenticity = _authenticity(verification.preflight, args.signature)
        qualification = qualification_report_for_verified_pack(
            verification,
            authenticity,
        )
        _print_json(
            {
                **verification.as_dict(),
                "qualification": qualification.as_dict(),
                "status": "VERIFIED",
            }
        )
        return 0
    if args.pack_action == "install":
        raw = read_pack_archive_bytes(args.archive)
        verification = verify_domain_pack_archive_bytes(raw)
        authenticity = _authenticity(verification.preflight, args.signature)
        qualification = qualification_report_for_verified_pack(
            verification,
            authenticity,
        )
        paths = DataPaths(args.data_root)
        paths.ensure(DataAreaId.STAGING)
        stage = stage_pack_archive_bytes(
            raw,
            paths.staging,
            expected_pack_id=verification.pack_id,
            expected_transport_sha256=verification.preflight.transport_sha256,
        )
        try:
            receipt = install_pack(
                stage,
                paths=paths,
                environment=runtime_environment_for_verified_pack_v1(verification),
            )
        except BaseException:
            # Installation may already have moved this exact stage into an inactive
            # content-addressed orphan before a later registry failure.  Cleanup is
            # consequently best-effort and remains restricted to this capability.
            try:
                discard_pack_stage(stage)
            except Exception:
                pass
            raise
        if not receipt.installed_new_object:
            discard_pack_stage(stage)
        _print_json(
            {
                "domain_identity_sha256": verification.index.domain_identity_sha256,
                "install_receipt": receipt.as_dict(),
                "qualification": qualification.as_dict(),
                "status": "INSTALLED",
            }
        )
        return 0
    if args.pack_action == "list":
        registry = read_pack_registry(paths=DataPaths(args.data_root))
        _print_json(
            {
                "entries": [item.as_dict() for item in registry.entries],
                "entry_count": len(registry.entries),
                "registry_sha256": registry.sha256,
                "schema_id": registry.schema_id,
                "schema_version": registry.schema_version,
            }
        )
        return 0
    if args.pack_action == "remove":
        paths = DataPaths(args.data_root)
        registry = read_pack_registry(paths=paths)
        matches = tuple(
            item for item in registry.entries if item.pack_id == args.pack_id
        )
        if len(matches) != 1:
            raise ValueError(
                "PACK_NOT_INSTALLED: exact logical pack ID is absent from the registry"
            )
        deactivation = deactivate_pack(matches[0].key, paths=paths)
        removal = remove_deactivated_pack(matches[0].key, paths=paths)
        _print_json(
            {
                "deactivation_receipt": deactivation.as_dict(),
                "removal_receipt": removal.as_dict(),
                "status": "REMOVED_TO_RECOVERY",
            }
        )
        return 0
    raise RuntimeError("pack action is not exhaustively handled")


def _configure_pack_build_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", required=True, dest="pack_demo_type")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", type=Path)


def _handle_pack_build_demo(args: argparse.Namespace) -> int:
    if args.pack_demo_type.casefold() != "scenario":
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.UNSUPPORTED_PACK_TYPE,
            "WO39-D1 build demo currently accepts the canonical scenario source adapter",
        )
    specification, payloads = build_scenario_demo_inputs(args.source)
    build = build_domain_pack(specification, payloads)
    target: Path | None = None
    if args.output is not None:
        target = write_new_pack_archive(build, args.output)
    result = _build_result(build, target)
    result.update(
        {
            "demo_type": "scenario",
            "status": "PASS",
        }
    )
    _print_json(result)
    return 0


def _configure_pack_portability_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample-set", required=True, type=Path)
    parser.add_argument("--hostile-set", required=True, type=Path)
    parser.add_argument("--seed", required=True, type=int)


def _handle_pack_portability_demo(args: argparse.Namespace) -> int:
    from kirby2.audit.pack_portability import run_pack_portability_demo

    report = run_pack_portability_demo(
        args.sample_set,
        args.hostile_set,
        seed=args.seed,
    )
    _print_json(report)
    return 0 if report["status"] == "PASS" else 1


def _default_output(build: DomainPackBuildV1) -> Path:
    return Path.cwd() / f"{build.manifest.name}-{build.manifest.version}.k2pack"


def _build_result(build: DomainPackBuildV1, target: Path | None) -> dict[str, object]:
    return {
        "archive_byte_count": len(build.archive_bytes),
        "domain_identity_sha256": build.index.domain_identity_sha256,
        "original_artifact_count": len(build.index.artifacts),
        "output_path": None if target is None else str(target.resolve()),
        "pack_id": build.manifest.pack_id,
        "pack_type": build.manifest.pack_type.value,
        "transport_sha256": build.transport_sha256,
    }


def _authenticity(
    preflight: PackArchivePreflightV1,
    signature_path: Path | None,
) -> PackAuthenticityVerificationV1:
    """Report detached authenticity without loading pack-supplied provider code."""

    signature = (
        None
        if signature_path is None
        else read_pack_signature_bytes(signature_path)
    )
    return verify_pack_signature(preflight, signature, providers={})


def _print_json(value: object) -> None:
    print(canonical_json_bytes(value).decode("ascii"))


PACK_COMMAND_MODULE = CommandModule(
    module_id="DOMAIN_PACKS",
    commands=(
        CommandSpec(
            command_id="PACK_LIFECYCLE",
            name="pack",
            help="build, export, inspect, verify, install, list, or remove data-only packs",
            handler=_handle_pack,
            configure=_configure_pack,
        ),
        CommandSpec(
            command_id="PACK_BUILD_DEMO",
            name="pack-build-demo",
            help="build and verify one canonical domain-pack demonstration",
            handler=_handle_pack_build_demo,
            configure=_configure_pack_build_demo,
        ),
        CommandSpec(
            command_id="PACK_PORTABILITY_DEMO",
            name="pack-portability-demo",
            help="qualify governed sample packs across offline clean roots",
            handler=_handle_pack_portability_demo,
            configure=_configure_pack_portability_demo,
        ),
    ),
)


__all__ = ["PACK_COMMAND_MODULE"]
